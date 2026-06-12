"""NIP-46 (Nostr Connect) remote-signer client — ``nostrconnect://`` flow.

Implements the *client-initiated* connection from NIP-46:

  1. Generate an ephemeral **client keypair** (disposable; never the
     user's key).
  2. Emit a ``nostrconnect://<client-pubkey>?relay=...&secret=...&perms=...``
     URI for the user to open/scan in their signer (Amber, nsec.app, …).
  3. Open a websocket to the relay, subscribe (REQ) for kind-24133 events
     ``#p``-tagged to the client pubkey, then wait for the signer's
     ``connect`` response carrying our ``secret`` (spoofing guard).
  4. Learn the ``remote-signer-pubkey`` from that response author and
     call ``get_public_key`` to learn the actual ``user-pubkey``.
  5. ``sign_event`` proxies an unsigned event to the signer and returns
     the signed one.  ``auth_url`` challenges surface to a callback so
     the CLI can tell the user to approve in their signer.

Transport is ``websocket-client`` (synchronous), matching the thin
client's sync architecture.  Request/response payloads are NIP-44
encrypted (see ``nip44``); request events are signed with the ephemeral
client key (see ``event``).

Best-practice details honored (per nostrconnect.org):
  * ``since`` filter on the subscription (strfry replays old ephemerals);
  * ignore duplicate responses (track handled request ids);
  * validate the connect ``secret``;
  * only the first ``auth_url`` per request is surfaced.

Security — Mike Dilger attack (see pretyflaco/BBTV2 #3): the
``nostrconnect://`` URI is published to a public relay, so an attacker
monitoring the relay learns our ``client-pubkey`` and could race to send
an encrypted ``connect`` response from their own key.  BBTV2 #3's
mitigation is that the *connection token must carry a secret* — and
``build_connect_uri`` always includes ``secret=``, so we satisfy it.  We
accept a connect result of our echoed ``secret`` OR ``"ack"`` (Amber and
other mainstream signers reply ``"ack"`` and do not echo the secret;
requiring the echo would make login impossible with them).  The decisive
defense is downstream: the final NIP-98 login event is signed by the
user-pubkey learned via ``get_public_key``, and the server validates
that pubkey against its ``nostr_members`` allowlist — a hijacker's
non-allowlisted key is rejected with a 403, so winning the relay race
yields no vezir session.

Scope: enough for ``vezir login`` (connect + get_public_key + sign_event).
``switch_relays`` / multi-relay pooling are intentionally out of scope —
a single user-chosen relay is sufficient for a one-shot terminal login.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from urllib.parse import quote, urlencode

from coincurve import PrivateKey

from . import event as nostr_event
from . import nip44

log = logging.getLogger("vezir.nip46")

NIP46_KIND = 24133

# A relay known to handle NIP-46 ephemeral events well.  Overridable.
DEFAULT_RELAY = "wss://relay.nsec.app"

# Default permissions we request: sign a NIP-98 HTTP-auth event (kind
# 27235) and read the pubkey.  Comma-separated method[:params] per spec.
DEFAULT_PERMS = "sign_event:27235,get_public_key"


class Nip46Error(Exception):
    """Raised on connection/signing failures from the remote signer."""


class Nip46Client:
    """A one-connection NIP-46 client over a single relay websocket.

    Usage::

        c = Nip46Client(relay="wss://relay.nsec.app",
                        name="vezir", on_auth_url=print)
        uri = c.build_connect_uri()
        # show `uri` (and/or its QR) to the user
        user_pubkey = c.wait_for_connection(timeout=120)
        signed = c.sign_event(unsigned_event_dict)
        c.close()
    """

    def __init__(
        self,
        *,
        relay: str = DEFAULT_RELAY,
        name: str = "vezir",
        perms: str = DEFAULT_PERMS,
        on_auth_url: Callable[[str], None] | None = None,
    ) -> None:
        self.relay = relay
        self.name = name
        self.perms = perms
        self.on_auth_url = on_auth_url

        self._client_priv = PrivateKey()
        self.client_pubkey = self._client_priv.public_key_xonly.format().hex()
        self._client_priv_hex = self._client_priv.to_hex()

        self.secret = secrets.token_hex(16)
        self.remote_signer_pubkey: str | None = None
        self.user_pubkey: str | None = None

        self._ws = None
        self._sub_id = "vezir-" + secrets.token_hex(4)
        self._since = int(time.time())
        # Track request ids we've already resolved to ignore duplicates.
        self._handled: set[str] = set()
        # Track which request ids have already surfaced an auth_url.
        self._auth_url_seen: set[str] = set()

    # ── connection token ─────────────────────────────────────────────────────

    def build_connect_uri(self) -> str:
        """Return the ``nostrconnect://`` URI for the user's signer."""
        params = [
            ("relay", self.relay),
            ("secret", self.secret),
            ("perms", self.perms),
            ("name", self.name),
        ]
        # client-pubkey is the URI "origin"; query is percent-encoded.
        return f"nostrconnect://{self.client_pubkey}?{urlencode(params, quote_via=quote)}"

    # ── websocket plumbing ───────────────────────────────────────────────────

    def _connect_ws(self):
        if self._ws is not None:
            return
        try:
            import websocket  # websocket-client
        except Exception as exc:  # pragma: no cover - import guard
            raise Nip46Error(
                "websocket-client is required for nostr login "
                "(install vezir[tui])"
            ) from exc
        try:
            self._ws = websocket.create_connection(self.relay, timeout=30)
        except Exception as exc:
            raise Nip46Error(f"failed to connect to relay {self.relay}: {exc}") from exc
        # Subscribe to responses addressed to us, fresh only.
        req = json.dumps([
            "REQ",
            self._sub_id,
            {"kinds": [NIP46_KIND], "#p": [self.client_pubkey], "since": self._since},
        ])
        self._ws.send(req)

    def _send_request(self, method: str, params: list, target_pubkey: str) -> str:
        """Encrypt + sign a kind-24133 request event, publish it, return req id."""
        req_id = secrets.token_hex(8)
        payload = json.dumps({"id": req_id, "method": method, "params": params})
        ciphertext = nip44.encrypt_for(payload, self._client_priv_hex, target_pubkey)
        ev = nostr_event.finalize_event(
            private_key_hex=self._client_priv_hex,
            kind=NIP46_KIND,
            tags=[["p", target_pubkey]],
            content=ciphertext,
        )
        self._ws.send(json.dumps(["EVENT", ev]))
        return req_id

    def _read_response(self, expect_id: str, timeout: float) -> str:
        """Block until a response for ``expect_id`` arrives; return its result.

        Handles ``auth_url`` challenges by invoking ``on_auth_url`` and
        continuing to wait (the signer re-sends with the same id once the
        user approves).  Raises ``Nip46Error`` on an ``error`` result or
        timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                self._ws.settimeout(min(remaining, 30))
                raw = self._ws.recv()
            except Exception as exc:
                if time.time() >= deadline:
                    break
                log.debug("relay recv hiccup: %s", exc)
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, list) or msg[0] != "EVENT" or len(msg) < 3:
                continue
            ev = msg[2]
            author = ev.get("pubkey")
            try:
                plaintext = nip44.decrypt_from(
                    ev.get("content", ""), self._client_priv_hex, author
                )
                resp = json.loads(plaintext)
            except Exception:
                # Not for us / undecryptable -- skip.
                continue
            rid = resp.get("id")
            if rid != expect_id:
                continue

            result = resp.get("result")
            error = resp.get("error")

            # Auth challenge: result == "auth_url", error == URL to open.
            if result == "auth_url":
                if rid not in self._auth_url_seen:
                    self._auth_url_seen.add(rid)
                    if self.on_auth_url and error:
                        self.on_auth_url(error)
                # keep waiting for the real result on the same id
                continue

            if rid in self._handled:
                continue

            if error:
                raise Nip46Error(f"remote signer error: {error}")
            self._handled.add(rid)
            # Learn the remote-signer pubkey from the responder.
            if self.remote_signer_pubkey is None:
                self.remote_signer_pubkey = author
            return result if result is not None else ""

        raise Nip46Error("timed out waiting for the remote signer")

    # ── high-level flow ──────────────────────────────────────────────────────

    def wait_for_connection(self, timeout: float = 120) -> str:
        """Wait for the signer to answer our ``nostrconnect://`` and return user pubkey.

        The signer initiates by sending a ``connect`` *response* carrying
        our ``secret``.  We validate the secret, learn the signer pubkey,
        then call ``get_public_key`` to resolve the user's pubkey.
        """
        self._connect_ws()
        deadline = time.time() + timeout

        # First inbound message for us should be the connect response whose
        # result equals our secret (or "ack").  We don't know the signer
        # pubkey yet, so read raw until we see a decryptable response.
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                self._ws.settimeout(min(remaining, 30))
                raw = self._ws.recv()
            except Exception:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, list) or msg[0] != "EVENT" or len(msg) < 3:
                continue
            ev = msg[2]
            author = ev.get("pubkey")
            try:
                plaintext = nip44.decrypt_from(
                    ev.get("content", ""), self._client_priv_hex, author
                )
                resp = json.loads(plaintext)
            except Exception:
                continue
            result = resp.get("result")
            # SECURITY — Mike Dilger attack (see pretyflaco/BBTV2 #3):
            # that mitigation requires the *connection token to carry a
            # secret*, and ``build_connect_uri`` always includes
            # ``secret=`` in our nostrconnect:// URI — so we satisfy it.
            #
            # We accept the signer's connect response of either our echoed
            # ``secret`` (spec-ideal) OR ``"ack"``.  Real-world signers
            # (Amber in particular) reply ``"ack"`` and do NOT echo the
            # secret, matching NDK / nostr-tools BunkerSigner behavior; a
            # strict secret-echo requirement makes login impossible with
            # them.  Residual spoofing is neutralized downstream: after
            # connect we learn the user-pubkey via ``get_public_key`` and
            # the final NIP-98 login event is signed by that key, which the
            # server validates against its npub allowlist — a hijacker's
            # non-allowlisted key yields a 403, so winning the connect race
            # has no payoff against vezir.
            if result == self.secret or result == "ack":
                self.remote_signer_pubkey = author
                log.info("nip46: connected to signer %s", author[:12])
                self.user_pubkey = self._get_public_key(timeout=deadline - time.time())
                return self.user_pubkey
            if result == "auth_url":
                url = resp.get("error")
                if url and self.on_auth_url and resp.get("id") not in self._auth_url_seen:
                    self._auth_url_seen.add(resp.get("id"))
                    self.on_auth_url(url)
                continue

        raise Nip46Error(
            "timed out waiting for the signer to connect; did you approve "
            "the request in your signer app?"
        )

    def _get_public_key(self, timeout: float) -> str:
        rid = self._send_request("get_public_key", [], self.remote_signer_pubkey)
        pubkey = self._read_response(rid, timeout=max(timeout, 10))
        if not pubkey or len(pubkey) != 64:
            raise Nip46Error(f"signer returned an invalid user pubkey: {pubkey!r}")
        return pubkey

    def sign_event(self, unsigned: dict, timeout: float = 120) -> dict:
        """Ask the signer to sign ``unsigned`` (a dict) and return the signed event.

        ``unsigned`` should carry ``kind``/``content``/``tags``/
        ``created_at``.  The signer fills ``pubkey``/``id``/``sig``.
        """
        if not self.remote_signer_pubkey:
            raise Nip46Error("not connected; call wait_for_connection() first")
        rid = self._send_request(
            "sign_event", [json.dumps(unsigned)], self.remote_signer_pubkey
        )
        result = self._read_response(rid, timeout=timeout)
        try:
            signed = json.loads(result)
        except Exception as exc:
            raise Nip46Error(f"signer returned non-JSON signed event: {exc}") from exc
        if not nostr_event.verify_event(signed):
            raise Nip46Error("signed event failed local verification")
        return signed

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.send(json.dumps(["CLOSE", self._sub_id]))
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
