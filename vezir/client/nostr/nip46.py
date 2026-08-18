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
  * no ``since`` on the response subscription — the client key is fresh
    per session (no history to exclude) and a relay-side ``since`` would
    drop responses from signers with skewed clocks;
  * ignore duplicate responses (track handled request ids);
  * validate the connect ``secret``;
  * only the first ``auth_url`` per request is surfaced.

Security — Mike Dilger attack (see pretyflaco/BBTV2 #3): the
``nostrconnect://`` URI is published to a public relay, so an attacker
monitoring the relay learns our ``client-pubkey`` and could race to send
an encrypted ``connect`` response from their own key.  BBTV2 #3's
mitigation is that the *connection token must carry a secret* — and
``build_connect_uri`` always includes ``secret=``, so we satisfy it.  We
accept a connect result of our echoed ``secret`` OR ``"ack"``: real Amber
(observed live, 2026-06-13) actually *echoes the secret*, while NDK /
nostr-tools-based signers reply ``"ack"`` — accepting both keeps login
working across signers.  The decisive defense is downstream: the final
NIP-98 login event is signed by the user-pubkey learned via
``get_public_key``, and the server validates that pubkey against its
``nostr_members`` allowlist — a hijacker's non-allowlisted key is
rejected with a 403, so winning the relay race yields no vezir session.

Scope: enough for ``vezir login`` (connect + get_public_key + sign_event).
We fan out across several relays (``DEFAULT_RELAYS``, blink's proven set)
for redundant delivery of the signer's ephemeral kind-24133 responses;
``switch_relays`` (signer asking us to move relays) is still out of scope
for a one-shot terminal login.
"""
from __future__ import annotations

import hmac
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

# Max |clock offset| (seconds) we'll learn from the signer's connect event.
# Real skew between a misconfigured client and an NTP-correct signer is
# usually seconds-to-minutes; anything beyond this is treated as bogus and
# ignored (don't let a hostile signer push our event timestamps absurdly
# far).  5 minutes comfortably covers realistic unsynced-clock drift.
MAX_CLOCK_OFFSET = 300

# Relays known to handle NIP-46 ephemeral events well.  This is blink
# POS / blink-terminal's exact set (NostrConnectService.ts
# DEFAULT_NIP46_RELAYS), which is proven to work reliably with Amber.
#
# WHY MULTIPLE RELAYS MATTERS (root cause of the old single-relay hang):
# Amber publishes each response (connect ack, get_public_key, sign_event)
# to the relays it parsed from our nostrconnect:// URI and stored against
# the connection (Amber BunkerRequestUtils.kt -> application.relays;
# EventNotificationConsumer.kt sends responses there).  kind-24133 events
# are ephemeral, so if the single relay drops/evicts a response we never
# see it and time out right after connect.  Advertising several relays
# gives the response redundant delivery paths — exactly why blink "always
# works".  We fan out: open all relays, broadcast each request to all, and
# accept the first copy of each response (de-duped by event id).
DEFAULT_RELAYS: list[str] = [
    "wss://relay.nsec.app",      # Popular NIP-46 relay
    "wss://relay.damus.io",      # Very reliable general relay
    "wss://nos.lol",             # Good uptime backup
    "wss://relay.getportal.cc",  # Portal relay
    "wss://offchain.pub",        # Offchain relay
]

# Back-compat alias: some callers/tests still pass a single ``relay=``.
DEFAULT_RELAY = DEFAULT_RELAYS[0]

# Default permissions we request: sign a NIP-98 HTTP-auth event (kind
# 27235) and read the pubkey.  Comma-separated method[:params] per spec.
DEFAULT_PERMS = "sign_event:27235,get_public_key"


class Nip46Error(Exception):
    """Raised on connection/signing failures from the remote signer."""


class Nip46Client:
    """A NIP-46 client fanned out across several relay websockets.

    Usage::

        c = Nip46Client(name="vezir", on_auth_url=print)   # uses DEFAULT_RELAYS
        uri = c.build_connect_uri()
        # show `uri` (and/or its QR) to the user
        user_pubkey = c.wait_for_connection(timeout=120)
        signed = c.sign_event(unsigned_event_dict)
        c.close()

    ``relays`` overrides the relay set; ``relay`` is accepted for
    back-compat (single relay -> one-element list).
    """

    def __init__(
        self,
        *,
        relays: list[str] | None = None,
        relay: str | None = None,
        name: str = "vezir",
        perms: str = DEFAULT_PERMS,
        on_auth_url: Callable[[str], None] | None = None,
        url: str | None = None,
        image: str | None = None,
    ) -> None:
        # Relay resolution precedence: explicit ``relays`` list, else a
        # single back-compat ``relay``, else the proven default set.
        if relays:
            self.relays = list(relays)
        elif relay:
            self.relays = [relay]
        else:
            self.relays = list(DEFAULT_RELAYS)
        # Back-compat attribute some callers/tests read.
        self.relay = self.relays[0]
        self.name = name
        self.perms = perms
        self.on_auth_url = on_auth_url
        # App identity metadata for the signer's consent screen.  ``url``
        # is the app origin (server BASE URL, not the login endpoint):
        # signers that origin-bind a ``sign_event:27235`` grant compare its
        # host against the NIP-98 ``u``-tag host.  ``image`` is an optional
        # avatar shown next to the app name.
        self.url = url
        self.image = image

        self._client_priv = PrivateKey()
        self.client_pubkey = self._client_priv.public_key_xonly.format().hex()
        self._client_priv_hex = self._client_priv.to_hex()

        self.secret = secrets.token_hex(16)
        self.remote_signer_pubkey: str | None = None
        self.user_pubkey: str | None = None

        # Open websocket per relay: {relay_url: ws}.  A request is
        # broadcast to all; the first decryptable copy of each response
        # wins (others are de-duped by event id via ``_seen_events``).
        self._wss: dict[str, object] = {}
        # Tests inject a single fake socket via ``client._ws``; honoring it
        # keeps the existing in-process FakeWS tests working unchanged.
        self._ws = None
        self._sub_id = "vezir-" + secrets.token_hex(4)
        # A SINGLE persistent subscription for the whole session (mirrors
        # nostr-tools setupSubscription).  We filter by #p (events addressed
        # to us) and never CLOSE/REQ again mid-flow — re-subscribing on a
        # flaky link races against our own publishes and was observed to
        # make the signer never receive get_public_key/sign_event.  Author
        # filtering (only the signer's replies) is done in software in
        # _read_response instead of via a relay-side authors=[] filter.
        self._subscribed = False
        # Event ids already processed, so the same response arriving on
        # multiple relays is handled once.
        self._seen_events: set[str] = set()
        # No relay-side ``since`` on the subscription: the filter is computed
        # from OUR clock, but responses are stamped with the SIGNER's clock.
        # A signer more than ~60s behind us had every response relay-filtered,
        # making login take the full republish/timeout budget (~2 min).
        # The client keypair is generated fresh per session, so no historical
        # events can be addressed to this #p pubkey and ``since`` has nothing
        # to exclude — the btcpay-nostr-login plugin uses a plain #p filter
        # for the same reason.  Clock-skew correction still applies to our
        # OUTGOING created_at (see _learn_clock_offset).
        # Which encryption the peer (signer) uses for kind-24133 — learned
        # from the first response we decrypt.  Real Amber (observed live)
        # uses "nip44"; some older signers/builds use "nip04".  We
        # auto-detect (see _decrypt_any) and reply in the same scheme.
        # Default to nip44 for our first outgoing message; corrected once
        # we know.
        self._peer_scheme: str = "nip44"
        # Track request ids we've already resolved to ignore duplicates.
        self._handled: set[str] = set()
        # Track which request ids have already surfaced an auth_url.
        self._auth_url_seen: set[str] = set()
        # Clock-skew correction.  Amber subscribes for OUR requests with a
        # relay-side filter `since = now` (the signer's clock).  If our
        # clock is behind the signer's, every request we stamp with
        # created_at = our_now is filtered out by the relay before reaching
        # the signer — the request is never delivered and the flow hangs
        # after connect (observed on a laptop with NTP disabled).  We learn
        # the offset from the signer's connect-response `created_at` (its
        # NTP-correct clock) and add it to our outgoing `created_at`, so our
        # events clear the signer's `since` regardless of local skew.
        # Clamped to a sane range to reject absurd/malicious values.
        self._clock_offset: int = 0

    # ── connection token ─────────────────────────────────────────────────────

    def build_connect_uri(self) -> str:
        """Return the ``nostrconnect://`` URI for the user's signer.

        Each relay is emitted as its own repeated ``relay=`` query param
        (NIP-46 / nostr-tools ``getAll("relay")`` convention).  Amber
        stores ALL of them and replies on each, which is what gives the
        signer's responses redundant delivery paths.
        """
        params = [("relay", r) for r in self.relays]
        params += [
            ("secret", self.secret),
            ("perms", self.perms),
            ("name", self.name),
        ]
        if self.url:
            params.append(("url", self.url))
        if self.image:
            params.append(("image", self.image))
        # client-pubkey is the URI "origin"; query is percent-encoded.
        return f"nostrconnect://{self.client_pubkey}?{urlencode(params, quote_via=quote)}"

    # ── websocket plumbing ───────────────────────────────────────────────────

    def _sockets(self) -> list:
        """All live sockets to broadcast over / read from.

        When a test injects a single fake socket as ``self._ws`` we use
        only that (the in-process FakeWS round-trip).  Otherwise we use the
        per-relay connections opened by ``_connect_ws``.
        """
        if self._ws is not None:
            return [self._ws]
        return list(self._wss.values())

    def _open_relay(self, url: str):
        """Open one relay websocket (used at connect and on reconnect)."""
        import websocket  # websocket-client

        return websocket.create_connection(url, timeout=30)

    def _connect_ws(self):
        # A test-injected fake socket short-circuits real networking.
        if self._ws is not None:
            self._subscribe_once()
            return
        if self._wss:
            return
        try:
            import websocket  # noqa: F401  (import guard)
        except Exception as exc:  # pragma: no cover - import guard
            raise Nip46Error(
                "websocket-client is required for nostr login "
                "(install vezir[tui])"
            ) from exc

        # Open every relay concurrently; tolerate partial failure (blink
        # behaves the same — one slow/dead relay must not block login).  We
        # only hard-fail if NOT A SINGLE relay connects.
        import threading

        errors: dict[str, str] = {}
        lock = threading.Lock()

        def _open(url: str) -> None:
            try:
                ws = self._open_relay(url)
            except Exception as exc:  # relay down / blocked — skip it
                with lock:
                    errors[url] = str(exc)
                return
            with lock:
                self._wss[url] = ws

        threads = [threading.Thread(target=_open, args=(u,)) for u in self.relays]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not self._wss:
            detail = "; ".join(f"{u}: {e}" for u, e in errors.items())
            raise Nip46Error(f"failed to connect to any relay ({detail})")
        if errors:
            for url, exc in errors.items():
                log.debug("relay %s unavailable: %s", url, exc)
        log.info(
            "nip46: connected to %d/%d relays", len(self._wss), len(self.relays)
        )
        self._subscribe_once()

    def _reconnect_dead_relays(self) -> None:
        """Reopen any advertised relay that isn't currently connected.

        Handles relays that refused at startup (e.g. transient DNS failure
        or ``Connection refused``) or dropped mid-session, so the signer's
        relay set and ours keep overlapping.  Best-effort, non-blocking on
        failure.  Re-subscribes freshly reconnected sockets.
        """
        if self._ws is not None:
            return  # test-injected single fake socket: nothing to reconnect
        missing = [u for u in self.relays if u not in self._wss]
        if not missing:
            return
        for url in missing:
            try:
                ws = self._open_relay(url)
            except Exception as exc:
                log.debug("relay %s still unavailable: %s", url, exc)
                continue
            self._wss[url] = ws
            log.info("nip46: (re)connected relay %s", url)
            # Subscribe the freshly opened socket so it delivers responses.
            try:
                ws.send(self._req_msg())
            except Exception as exc:
                log.debug("REQ on reconnected %s failed: %s", url, exc)

    def _req_msg(self) -> str:
        """The single REQ subscription message (kind-24133, #p == us).

        Deliberately NO ``since`` (see __init__): a fresh ephemeral client
        pubkey has no history to exclude, and a relay-side ``since`` computed
        from our clock would drop responses stamped by a skewed signer clock.
        """
        filt: dict = {
            "kinds": [NIP46_KIND],
            "#p": [self.client_pubkey],
        }
        return json.dumps(["REQ", self._sub_id, filt])

    def _subscribe_once(self) -> None:
        """Issue the ONE persistent REQ subscription on every live socket.

        Filtered by ``#p`` (events addressed to us) — which already covers
        the signer's connect/get_public_key/sign_event responses (they're
        #p-tagged to our client pubkey).  We intentionally do NOT add a
        relay-side ``authors=[signer]`` filter nor ever CLOSE/re-REQ during
        the session: re-subscribing on a flaky link races against our own
        publishes and was observed to make the signer never receive our
        requests.  Author filtering happens in software in _read_response.
        """
        if self._subscribed and self._ws is None:
            return
        req_msg = self._req_msg()
        for ws in self._sockets():
            try:
                ws.send(req_msg)
            except Exception as exc:
                log.debug("REQ send failed on a relay: %s", exc)
        self._subscribed = True

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

    def _build_request_event(
        self, method: str, params: list, target_pubkey: str
    ) -> tuple[str, str]:
        """Encrypt + sign a kind-24133 request event.

        Returns ``(req_id, event_json_msg)`` where ``event_json_msg`` is the
        ready-to-send ``["EVENT", ev]`` frame.  Kept separate from
        publishing so the wait loop can RE-broadcast the same event on a
        flaky network (the signer must *receive* it; a single ws.send that
        the local OS accepts does not guarantee the relay delivered it).
        """
        req_id = secrets.token_hex(8)
        payload = json.dumps({"id": req_id, "method": method, "params": params})
        ciphertext = self._encrypt_for_peer(payload, target_pubkey)
        # Stamp with the signer-corrected clock so the event clears the
        # signer's `since` request-filter even if our local clock is behind.
        created_at = int(time.time()) + self._clock_offset
        ev = nostr_event.finalize_event(
            private_key_hex=self._client_priv_hex,
            kind=NIP46_KIND,
            tags=[["p", target_pubkey]],
            content=ciphertext,
            created_at=created_at,
        )
        return req_id, json.dumps(["EVENT", ev])

    @property
    def clock_offset(self) -> int:
        """Clock skew (seconds) learned from the signer's connect event.

        Add this to ``int(time.time())`` when stamping ``created_at`` on any
        event the signer will sign but the SERVER will validate for freshness
        (e.g. the NIP-98 login event) — on a skewed-clock machine the raw
        local time would otherwise fall outside the server's freshness window
        and the login POST would 401 after an otherwise-successful handshake.
        """
        return self._clock_offset

    def _learn_clock_offset(self, signer_created_at) -> None:
        """Learn our clock offset from the signer's connect-event time.

        ``offset = signer_created_at - our_now``.  Applied to outgoing
        request ``created_at`` so our events clear the signer's
        ``since``-filtered subscription regardless of local clock skew.
        Clamped to ``±MAX_CLOCK_OFFSET``; ignored if non-numeric.
        """
        try:
            sc = int(signer_created_at)
        except (TypeError, ValueError):
            return
        offset = sc - int(time.time())
        if offset > MAX_CLOCK_OFFSET:
            offset = MAX_CLOCK_OFFSET
        elif offset < -MAX_CLOCK_OFFSET:
            offset = -MAX_CLOCK_OFFSET
        self._clock_offset = offset
        if abs(offset) >= 5:
            log.info(
                "nip46: local clock is %+ds vs the signer; correcting "
                "request timestamps (consider enabling NTP)", -offset,
            )

    def _publish(self, event_msg: str, *, reconnect: bool = True) -> int:
        """Broadcast one ``["EVENT", ev]`` frame to every live relay.

        Returns the number of relays the frame was written to.  When
        ``reconnect`` is set, first try to reopen any relay that's currently
        down, so the signer's relay set and ours keep overlapping (the
        observed laptop failure was the signer never receiving our request
        because too few shared, live relays carried it).
        """
        if reconnect:
            self._reconnect_dead_relays()
        sent = 0
        for ws in self._sockets():
            try:
                ws.send(event_msg)
                sent += 1
            except Exception as exc:
                log.debug("EVENT send failed on a relay: %s", exc)
        return sent

    def _send_request(self, method: str, params: list, target_pubkey: str) -> str:
        """Encrypt + sign a kind-24133 request event, publish it, return req id."""
        req_id, event_msg = self._build_request_event(method, params, target_pubkey)
        if self._publish(event_msg) == 0:
            raise Nip46Error("failed to publish request to any relay")
        return req_id

    def _recv_raw(self, remaining: float) -> str | None:
        """Return the next raw relay message across all sockets, or None.

        Polls every live socket round-robin with a short per-socket
        timeout so one quiet relay never starves the others (synchronous
        ``websocket-client`` has no shared select).  De-dupes EVENTs by
        event id, since the same kind-24133 response is delivered by each
        relay that has it — we only want to process the first copy.
        """
        sockets = self._sockets()
        if not sockets:
            return None
        # Short per-socket slice so a full sweep across all sockets stays
        # well under the republish interval (a quiet relay must not starve
        # the others or delay re-publishing).  Total sweep ~= len*per_sock.
        per_sock = max(0.05, min(remaining / max(len(sockets), 1), 0.3))
        for ws in sockets:
            try:
                ws.settimeout(per_sock)
            except Exception:
                pass
            try:
                raw = ws.recv()
            except Exception:
                continue  # timeout/hiccup on this socket — try the next
            if not raw:
                continue
            # Drop duplicate EVENTs (same id from another relay) early.
            try:
                msg = json.loads(raw)
                if (
                    isinstance(msg, list)
                    and msg[0] == "EVENT"
                    and len(msg) >= 3
                    and isinstance(msg[2], dict)
                ):
                    eid = msg[2].get("id")
                    if eid is not None:
                        if eid in self._seen_events:
                            continue
                        self._seen_events.add(eid)
            except Exception:
                pass
            return raw
        return None

    def _read_response(
        self,
        expect_id: str,
        timeout: float,
        *,
        accept_any: bool = False,
        event_msg: str | None = None,
        republish_interval: float = 4.0,
    ) -> str:
        """Block until a response for ``expect_id`` arrives; return its result.

        Handles ``auth_url`` challenges by invoking ``on_auth_url`` and
        continuing to wait (the signer re-sends once the user approves).
        Raises ``Nip46Error`` on an ``error`` result or timeout.

        ``accept_any``: when True, accept the first result-bearing response
        from the signer regardless of whether its ``id`` matches.  This is
        a defensive fallback for signer variants that mint a fresh response
        id instead of echoing ours.  (Real Amber, observed live, DOES echo
        the request id — so id-matching normally works — but keeping the
        provenance fallback costs nothing and tolerates other signers.)
        We still skip pure connect echoes (``"ack"`` / our secret), which
        are not method results.

        ``event_msg``: when given, the request frame is RE-broadcast every
        ``republish_interval`` seconds while we wait (reconnecting any dead
        relay first) as a secondary safety net for a transient delivery
        drop or a relay that comes back mid-wait.  (The primary cause of a
        request never reaching the signer was clock skew vs the signer's
        ``since`` filter — corrected at build time; see _learn_clock_offset.)
        """
        deadline = time.time() + timeout
        last_publish = time.time()  # _send_request already published once
        republishes = 0
        while time.time() < deadline:
            # Re-broadcast the request periodically so a dropped/mis-routed
            # publish self-heals (and pick up any relay that came back).
            if event_msg is not None and (time.time() - last_publish) >= republish_interval:
                n = self._publish(event_msg)
                republishes += 1
                # Throttle the log: only every 5th re-publish (~20s) to
                # avoid flooding --verbose output.
                if republishes % 5 == 1:
                    log.debug("re-published request to %d relay(s) (x%d)", n, republishes)
                last_publish = time.time()
            remaining = deadline - time.time()
            raw = self._recv_raw(remaining)
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
            # Auth challenge: result == "auth_url", error == URL to open.
            # (Handle before id-matching: Amber may use a fresh id here.)
            if result == "auth_url":
                # Only honor HTTPS auth_urls from our bound signer (see the
                # phishing note in wait_for_connection).
                bound = self.remote_signer_pubkey
                ok = (
                    isinstance(error, str)
                    and error.lower().startswith("https://")
                    and (bound is None or author == bound)
                )
                if not ok:
                    log.warning(
                        "nip46: ignoring untrusted auth_url from %s",
                        (author or "?")[:8],
                    )
                    continue
                if rid not in self._auth_url_seen:
                    self._auth_url_seen.add(rid)
                    if self.on_auth_url:
                        self.on_auth_url(error)
                # keep waiting for the real result on the same id
                continue

            # Skip the signer's connect echoes ("ack" / our secret) — these
            # are not method results (Amber re-emits them repeatedly).
            if result == "ack" or (
                isinstance(result, str) and hmac.compare_digest(result, self.secret)
            ):
                log.debug("skip connect echo (result=%r) while awaiting reply", result)
                continue

            # Id matching: the spec (and real Amber, observed live) echoes
            # the request id.  ``accept_any`` is a defensive fallback for
            # signer variants that don't: with one request in flight and
            # relays filtered to the signer, accept its first result/error
            # reply by provenance.
            if rid != expect_id and not accept_any:
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
            raw = self._recv_raw(remaining)
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
            # ``secret`` (spec-ideal) OR ``"ack"``.  Observed live: real
            # Amber *echoes the secret*; NDK / nostr-tools BunkerSigner
            # reply ``"ack"`` — accepting both keeps login working across
            # signers (a strict secret-echo-only requirement would break
            # the ack signers).  Residual spoofing is neutralized downstream: after
            # connect we learn the user-pubkey via ``get_public_key`` and
            # the final NIP-98 login event is signed by that key, which the
            # server validates against its npub allowlist — a hijacker's
            # non-allowlisted key yields a 403, so winning the connect race
            # has no payoff against vezir.
            if (
                (isinstance(result, str)
                 and hmac.compare_digest(result, self.secret))
                or result == "ack"
            ):
                self.remote_signer_pubkey = author
                log.info("nip46: connected to signer %s", author[:12])
                # Learn the clock offset from the signer's connect event:
                # its `created_at` is stamped by the signer's (NTP-correct)
                # clock, which is also what its `since` request-subscription
                # uses.  Stamping our requests with this offset makes them
                # clear the signer's `since` even if our local clock is
                # behind.  Clamp to reject absurd/malicious skew.
                self._learn_clock_offset(ev.get("created_at"))
                # We do NOT re-subscribe here.  The single #p subscription
                # from _subscribe_once already receives the signer's
                # responses (they're #p-tagged to us); author filtering is
                # done in software.  Re-subscribing (CLOSE/REQ) on a flaky
                # link races our own publishes and was observed to make the
                # signer never receive get_public_key/sign_event.
                # Short stabilization delay before the first request, as
                # blink-terminal does (POST_CONNECT_DELAY).
                time.sleep(0.5)
                # Resolve the user pubkey via get_public_key — the proven
                # blink flow (NostrConnectService getPublicKeyWithRetry).
                # Requests are re-published while waiting (see
                # _read_response event_msg), so a transient delivery gap
                # self-heals.  If it still fails, we fall back to the pubkey
                # on the signed login event in sign_event().
                try:
                    self.user_pubkey = self._get_public_key(deadline)
                    log.info("nip46: user pubkey %s", (self.user_pubkey or "")[:12])
                    return self.user_pubkey
                except Nip46Error as exc:
                    log.debug(
                        "get_public_key failed (%s); will derive pubkey "
                        "from the signed event instead", exc,
                    )
                    self.user_pubkey = None
                    return author
            if result == "auth_url":
                # SECURITY: before a signer is bound, ANY author who can
                # encrypt to our (relay-visible) client pubkey can inject an
                # auth_url that we'd surface to the user as "open this link
                # to approve" — a phishing vector.  Only surface auth_urls
                # that (a) are HTTPS and (b) come from the author we've
                # already bound as our signer (or, pre-bind, defer until an
                # author is bound).  A mismatched/pre-bind/non-HTTPS auth_url
                # is logged and dropped.
                url = resp.get("error")
                bound = self.remote_signer_pubkey
                if not (isinstance(url, str) and url.lower().startswith("https://")):
                    log.warning(
                        "nip46: ignoring non-HTTPS auth_url from %s",
                        (author or "?")[:8],
                    )
                    continue
                if bound is not None and author != bound:
                    log.warning(
                        "nip46: ignoring auth_url from non-signer author %s",
                        (author or "?")[:8],
                    )
                    continue
                if self.on_auth_url and resp.get("id") not in self._auth_url_seen:
                    self._auth_url_seen.add(resp.get("id"))
                    self.on_auth_url(url)
                continue

        raise Nip46Error(
            "timed out waiting for the signer to connect; did you approve "
            "the request in your signer app?"
        )

    def _get_public_key(self, deadline: float, *, attempts: int = 3) -> str:
        """Ask the signer for the user pubkey (NIP-46 ``get_public_key``).

        Mirrors blink-terminal's ``getPublicKeyWithRetry``: retries a few
        times with backoff, bounded by the overall connect ``deadline``.
        Uses ``accept_any`` because Amber may answer with a fresh response
        id rather than echoing ours.  Returns the 64-hex pubkey.
        """
        if not self.remote_signer_pubkey:
            raise Nip46Error("not connected; cannot get_public_key")
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Per-attempt budget: split the remaining time but cap so a
            # single stalled attempt doesn't eat the whole window.
            budget = min(remaining, 20.0)
            rid, event_msg = self._build_request_event(
                "get_public_key", [], self.remote_signer_pubkey
            )
            if self._publish(event_msg) == 0:
                last_exc = Nip46Error("failed to publish get_public_key to any relay")
                log.debug("%s", last_exc)
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                continue
            try:
                result = self._read_response(
                    rid, timeout=budget, accept_any=True, event_msg=event_msg
                )
            except Nip46Error as exc:
                last_exc = exc
                log.debug("get_public_key attempt %d failed: %s", attempt, exc)
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                continue
            pk = (result or "").strip()
            # Basic sanity: a NIP-46 pubkey result is 64 lowercase hex.
            if len(pk) == 64 and all(c in "0123456789abcdef" for c in pk.lower()):
                return pk.lower()
            last_exc = Nip46Error(f"get_public_key returned non-pubkey: {result!r}")
            log.debug("%s", last_exc)
            if attempt < attempts:
                time.sleep(0.5 * attempt)
        raise last_exc or Nip46Error("get_public_key: no response")

    def sign_event(self, unsigned: dict, timeout: float = 120) -> dict:
        """Ask the signer to sign ``unsigned`` (a dict) and return the signed event.

        ``unsigned`` should carry ``kind``/``content``/``tags``/
        ``created_at``.  The signer fills ``pubkey``/``id``/``sig``.
        """
        if not self.remote_signer_pubkey:
            raise Nip46Error("not connected; call wait_for_connection() first")
        rid, event_msg = self._build_request_event(
            "sign_event", [json.dumps(unsigned)], self.remote_signer_pubkey
        )
        if self._publish(event_msg) == 0:
            raise Nip46Error("failed to publish sign_event to any relay")
        # accept_any: defensive fallback for signers that don't echo the
        # request id (real Amber does); take the signer's first
        # signed-event reply by provenance.  event_msg: re-publish while
        # waiting so a dropped/mis-routed request self-heals.
        result = self._read_response(
            rid, timeout=timeout, accept_any=True, event_msg=event_msg
        )
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
        close_msg = json.dumps(["CLOSE", self._sub_id])
        # Test-injected single fake socket.
        if self._ws is not None:
            try:
                self._ws.send(close_msg)
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Real per-relay connections.
        for ws in list(self._wss.values()):
            try:
                ws.send(close_msg)
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        self._wss.clear()
