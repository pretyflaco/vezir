#!/usr/bin/env python3
"""Local NIP-46 *signer* harness — an automated stand-in for Amber.

Purpose: validate vezir's ``Nip46Client`` end-to-end over REAL relay
websockets WITHOUT a phone.  This plays the signer side of the
``nostrconnect://`` flow exactly like Amber would:

  1. Parse a vezir ``nostrconnect://<client-pubkey>?relay=...&secret=...``
     URI (multiple ``relay=`` params supported).
  2. Connect to the relay(s), subscribe for kind-24133 events #p-tagged to
     OUR signer pubkey (the requests vezir publishes to us).
  3. Reply to ``connect`` with ``"ack"``, answer ``get_public_key`` with our
     pubkey, and ``sign_event`` by actually signing the requested event with
     our key.

Amber-quirk emulation (all toggleable) so a green run here strongly
predicts a green run against real Amber:

  --scheme nip04|nip44   Encryption for kind-24133 payloads (Amber=nip04).
  --no-echo-id           Mint fresh UUID response ids instead of echoing the
                         request id (some Amber versions do this).
  --reply-relays N       Publish our responses to only the FIRST N relays
                         from the URI (simulates Amber landing responses on a
                         subset / a single relay — the redundancy stress test).
  --connect-delay S      Wait S seconds before sending the connect ack.

This is a throwaway test tool (NOT shipped in the package).  It reuses
vezir's own crypto/event primitives so the wire format matches byte-for-byte.

Usage::

    # Terminal A — start the signer, paste the URI when prompted (or pass it):
    python scripts/nip46_signer_harness.py --uri 'nostrconnect://...' --scheme nip04 --no-echo-id

    # It prints its signer pubkey first; vezir will learn it from the connect author.

Typically driven by scripts/nip46_loopback_test.py, which spawns both sides.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from coincurve import PrivateKey

from vezir.client.nostr import event as nostr_event
from vezir.client.nostr import nip04, nip44

NIP46_KIND = 24133


def log(msg: str) -> None:
    print(f"[signer] {msg}", file=sys.stderr, flush=True)


class SignerHarness:
    def __init__(
        self,
        uri: str,
        *,
        scheme: str = "nip44",
        echo_id: bool = True,
        reply_relays: int | None = None,
        listen_relays: int | None = None,
        connect_delay: float = 0.0,
        priv_hex: str | None = None,
    ) -> None:
        self.scheme = scheme
        self.echo_id = echo_id
        self.connect_delay = connect_delay
        self._listen_relays_n = listen_relays

        self._priv = PrivateKey(bytes.fromhex(priv_hex)) if priv_hex else PrivateKey()
        self.pubkey = self._priv.public_key_xonly.format().hex()
        self._priv_hex = self._priv.to_hex()

        parsed = urlsplit(uri)
        # nostrconnect://<client-pubkey>?...  -> netloc carries the pubkey.
        self.client_pubkey = parsed.netloc or parsed.path.lstrip("/")
        q = parse_qs(parsed.query)
        self.relays: list[str] = q.get("relay", [])
        self.secret = (q.get("secret") or [""])[0]
        if not self.relays:
            raise SystemExit("harness: URI has no relay= params")

        # Which relays we PUBLISH responses on (Amber-subset emulation).
        self.reply_relays = (
            self.relays[:reply_relays] if reply_relays else list(self.relays)
        )
        # Which relays we SUBSCRIBE on (read the client's requests from).
        # Simulates the laptop failure: if the client publishes only to
        # relays the signer isn't reading, the signer never sees the
        # request.  The client's periodic re-publish + relay reconnect must
        # eventually land a copy on a relay in this listen set.
        self.listen_relays = (
            self.relays[:listen_relays] if listen_relays else None
        )
        self._wss: dict[str, object] = {}
        self._sub_id = "signer-" + secrets.token_hex(4)
        self._since = int(time.time()) - 60
        self._stop = threading.Event()
        self._handled_reqs: set[str] = set()
        self.signed_count = 0

    # ── crypto matching vezir's wire format ──────────────────────────────
    def _encrypt(self, plaintext: str, peer_pub: str) -> str:
        if self.scheme == "nip04":
            return nip04.encrypt(plaintext, self._priv_hex, peer_pub)
        return nip44.encrypt_for(plaintext, self._priv_hex, peer_pub)

    def _decrypt(self, content: str, peer_pub: str) -> str:
        # Accept whichever the client used (vezir's first request may be nip44).
        if nip04.is_nip04(content):
            try:
                return nip04.decrypt(content, self._priv_hex, peer_pub)
            except Exception:
                pass
        try:
            return nip44.decrypt_from(content, self._priv_hex, peer_pub)
        except Exception:
            return nip04.decrypt(content, self._priv_hex, peer_pub)

    # ── websocket plumbing ───────────────────────────────────────────────
    def _connect(self) -> None:
        import websocket

        for url in self.relays:
            try:
                ws = websocket.create_connection(url, timeout=20)
                self._wss[url] = ws
            except Exception as exc:
                log(f"could not connect {url}: {exc}")
        if not self._wss:
            raise SystemExit("harness: failed to connect to any relay")
        filt = {
            "kinds": [NIP46_KIND],
            "#p": [self.pubkey],
            "since": self._since,
        }
        req = json.dumps(["REQ", self._sub_id, filt])
        listen_set = self.listen_relays if self.listen_relays else list(self._wss)
        subscribed = []
        for url, ws in self._wss.items():
            if url not in listen_set:
                continue  # connected but NOT reading requests here (overlap-gap sim)
            try:
                ws.send(req)
                subscribed.append(url)
            except Exception as exc:
                log(f"REQ failed on {url}: {exc}")
        self._subscribed_urls = set(subscribed)
        log(f"pubkey={self.pubkey}")
        log(f"listening (reading requests) on {len(subscribed)}/{len(self._wss)} "
            f"connected relays: {subscribed}")
        log(f"will reply on {len(self.reply_relays)} relay(s): {self.reply_relays}")

    def _publish(self, ev: dict) -> None:
        out = json.dumps(["EVENT", ev])
        for url in self.reply_relays:
            ws = self._wss.get(url)
            if not ws:
                continue
            try:
                ws.send(out)
            except Exception as exc:
                log(f"publish failed on {url}: {exc}")

    def _make_response_event(self, payload: dict) -> dict:
        plaintext = json.dumps(payload)
        ct = self._encrypt(plaintext, self.client_pubkey)
        return nostr_event.finalize_event(
            private_key_hex=self._priv_hex,
            kind=NIP46_KIND,
            tags=[["p", self.client_pubkey]],
            content=ct,
        )

    def _send_connect_ack(self) -> None:
        if self.connect_delay:
            time.sleep(self.connect_delay)
        payload = {"id": "connect", "result": "ack", "error": None}
        self._publish(self._make_response_event(payload))
        log("sent connect ack")

    def _handle_request_event(self, ev: dict) -> None:
        author = ev.get("pubkey")
        if author != self.client_pubkey:
            return
        try:
            plaintext = self._decrypt(ev.get("content", ""), self.client_pubkey)
            req = json.loads(plaintext)
        except Exception as exc:
            log(f"undecryptable request: {exc}")
            return
        rid = req.get("id")
        method = req.get("method")
        if rid in self._handled_reqs:
            return
        self._handled_reqs.add(rid)
        log(f"request method={method} id={rid}")

        if method == "get_public_key":
            result = self.pubkey
        elif method == "sign_event":
            unsigned = json.loads(req["params"][0])
            signed = nostr_event.finalize_event(
                private_key_hex=self._priv_hex,
                kind=unsigned["kind"],
                tags=unsigned["tags"],
                content=unsigned.get("content", ""),
                created_at=unsigned.get("created_at"),
            )
            result = json.dumps(signed)
            self.signed_count += 1
        elif method == "connect":
            result = "ack"
        else:
            log(f"unhandled method {method}; replying ack")
            result = "ack"

        resp_id = rid if self.echo_id else str(uuid.uuid4())
        payload = {"id": resp_id, "result": result, "error": None}
        self._publish(self._make_response_event(payload))
        log(f"replied method={method} (resp_id={resp_id})")

    def run(self, timeout: float = 120) -> None:
        self._connect()
        self._send_connect_ack()
        deadline = time.time() + timeout
        # Round-robin poll only the sockets we SUBSCRIBED on (we only read
        # the client's requests where we're listening — the overlap-gap sim).
        subscribed = getattr(self, "_subscribed_urls", set(self._wss))
        while not self._stop.is_set() and time.time() < deadline:
            for url, ws in list(self._wss.items()):
                if url not in subscribed:
                    continue
                try:
                    ws.settimeout(0.3)
                    raw = ws.recv()
                except Exception:
                    continue
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if (
                    isinstance(msg, list)
                    and msg[0] == "EVENT"
                    and len(msg) >= 3
                    and isinstance(msg[2], dict)
                ):
                    self._handle_request_event(msg[2])
        log(f"done (signed {self.signed_count} event(s))")

    def close(self) -> None:
        self._stop.set()
        for ws in self._wss.values():
            try:
                ws.close()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", help="nostrconnect:// URI (else read from stdin)")
    ap.add_argument("--scheme", choices=["nip04", "nip44"], default="nip44")
    ap.add_argument("--no-echo-id", action="store_true",
                    help="mint fresh UUID response ids (Amber quirk)")
    ap.add_argument("--reply-relays", type=int, default=None,
                    help="reply on only the first N relays from the URI")
    ap.add_argument("--listen-relays", type=int, default=None,
                    help="read the client's requests on only the first N "
                         "relays (simulates the laptop overlap gap)")
    ap.add_argument("--connect-delay", type=float, default=0.0)
    ap.add_argument("--priv", help="signer private key hex (else random)")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    uri = args.uri or sys.stdin.readline().strip()
    if not uri.startswith("nostrconnect://"):
        raise SystemExit("expected a nostrconnect:// URI")

    h = SignerHarness(
        uri,
        scheme=args.scheme,
        echo_id=not args.no_echo_id,
        reply_relays=args.reply_relays,
        listen_relays=args.listen_relays,
        connect_delay=args.connect_delay,
        priv_hex=args.priv,
    )
    try:
        h.run(timeout=args.timeout)
    finally:
        h.close()


if __name__ == "__main__":
    main()
