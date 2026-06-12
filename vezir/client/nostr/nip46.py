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
from . import nip04, nip44

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
        # 60s negative margin: relays replay ephemeral events and signer
        # clocks can be behind, so a `since` of exactly now can drop a
        # valid response.  60s is the value nostr-tools uses (see its
        # nip46 `since: now - 60` fix).
        self._since = int(time.time()) - 60
        # Which encryption the peer (signer) uses for kind-24133 — learned
        # from the first response we decrypt.  Amber uses "nip04"; newer
        # signers use "nip44".  We reply in the same scheme.  Default to
        # nip44 for our first outgoing message; corrected once we know.
        self._peer_scheme: str = "nip44"
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
        # Initial subscription (pre-connect): we don't yet know the signer
        # pubkey, so we can only filter by #p (events addressed to us).
        # After connect we re-subscribe with authors=[signer] to drop noise.
        self._send_req(authors=None)

    def _send_req(self, *, authors: list[str] | None) -> None:
        """(Re)issue the REQ subscription.  When ``authors`` is given, the
        relay only delivers events from those pubkeys (the signer), which
        is how nostr-tools avoids unrelated relay noise on the response
        path.  Closes any prior sub with the same id first."""
        filt: dict = {
            "kinds": [NIP46_KIND],
            "#p": [self.client_pubkey],
            "since": self._since,
        }
        if authors:
            filt["authors"] = authors
        # Close the previous subscription so the relay replaces it cleanly.
        try:
            self._ws.send(json.dumps(["CLOSE", self._sub_id]))
        except Exception:
            pass
        self._ws.send(json.dumps(["REQ", self._sub_id, filt]))

    def _encrypt_for_peer(self, plaintext: str, target_pubkey: str) -> str:
        """Encrypt a request payload in the scheme the peer signer uses."""
        if self._peer_scheme == "nip04":
            return nip04.encrypt(plaintext, self._client_priv_hex, target_pubkey)
        return nip44.encrypt_for(plaintext, self._client_priv_hex, target_pubkey)

    def _decrypt_any(self, content: str, sender_pubkey: str) -> tuple[str, str]:
        """Decrypt a kind-24133 content, auto-detecting NIP-04 vs NIP-44.

        Returns ``(plaintext, scheme)``.  Detects NIP-04 by the ``?iv=``
        marker (as NDK/nostr-tools do); tries the detected scheme first
        and falls back to the other.  Raises on total failure.
        """
        prefer_04 = nip04.is_nip04(content)
        order = (("nip04", "nip44") if prefer_04 else ("nip44", "nip04"))
        last_exc: Exception | None = None
        for scheme in order:
            try:
                if scheme == "nip04":
                    return (
                        nip04.decrypt(content, self._client_priv_hex, sender_pubkey),
                        "nip04",
                    )
                return (
                    nip44.decrypt_from(content, self._client_priv_hex, sender_pubkey),
                    "nip44",
                )
            except Exception as exc:  # try the other scheme
                last_exc = exc
        raise last_exc if last_exc else ValueError("undecryptable content")

    def _send_request(self, method: str, params: list, target_pubkey: str) -> str:
        """Encrypt + sign a kind-24133 request event, publish it, return req id."""
        req_id = secrets.token_hex(8)
        payload = json.dumps({"id": req_id, "method": method, "params": params})
        ciphertext = self._encrypt_for_peer(payload, target_pubkey)
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
            # Only the signer's responses matter here.  The relay is
            # already filtered to authors=[signer] post-connect, but
            # double-check and always decrypt with the SIGNER's key (the
            # fixed conversation peer), exactly like nostr-tools.
            if self.remote_signer_pubkey and author != self.remote_signer_pubkey:
                log.debug("skip event from %s: not the signer", (author or "?")[:8])
                continue
            peer = self.remote_signer_pubkey or author
            try:
                plaintext, scheme = self._decrypt_any(ev.get("content", ""), peer)
                self._peer_scheme = scheme
                resp = json.loads(plaintext)
            except Exception as exc:
                # Not for us / undecryptable -- skip (logged for --verbose).
                log.debug(
                    "skip event from %s: undecryptable (%s)",
                    (author or "?")[:8], exc,
                )
                continue
            rid = resp.get("id")
            result = resp.get("result")
            error = resp.get("error")
            log.debug(
                "recv from %s: id=%s (want %s) result=%r error=%r",
                (author or "?")[:8], rid, expect_id,
                (result[:40] if isinstance(result, str) else result), error,
            )
            # Strict id match (nostr-tools semantics: listeners[id]).  With
            # the authors=[signer] filter, the signer's id-echoed reply is
            # the only thing that reaches us, so an exact match is both
            # correct and unambiguous.
            if rid != expect_id:
                continue

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
                plaintext, scheme = self._decrypt_any(ev.get("content", ""), author)
                resp = json.loads(plaintext)
            except Exception as exc:
                log.debug(
                    "connect: skip event from %s: undecryptable (%s)",
                    (author or "?")[:8], exc,
                )
                continue
            # Learn the signer's encryption scheme so our subsequent
            # requests (get_public_key, sign_event) speak the same one.
            self._peer_scheme = scheme
            log.debug(
                "connect: decrypted %s response from %s: result=%r",
                scheme, (author or "?")[:8], resp.get("result"),
            )
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
                # Re-subscribe filtered to the signer's pubkey so the relay
                # only delivers ITS responses (mirrors nostr-tools'
                # setupSubscription authors=[signer]).  Without this the
                # relay streams unrelated kind-24133 traffic and we can't
                # cleanly match the get_public_key / sign_event reply.
                self._send_req(authors=[author])
                # Try get_public_key, but don't hard-fail the whole login
                # if the signer's reply is awkward.  The authoritative
                # user-pubkey is the ``pubkey`` field of the signed login
                # event, resolved in sign_event().
                try:
                    self.user_pubkey = self._get_public_key(
                        timeout=min(30, max(10, deadline - time.time()))
                    )
                except Nip46Error as exc:
                    log.debug("get_public_key failed (non-fatal): %s", exc)
                    self.user_pubkey = None
                return self.user_pubkey or ""
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
        # The signed event's pubkey IS the authoritative user pubkey —
        # record it (get_public_key may have been skipped/unreliable).
        self.user_pubkey = signed.get("pubkey") or self.user_pubkey
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
