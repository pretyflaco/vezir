#!/usr/bin/env python3
"""Layer 3: live NIP-46 handshake against REAL Amber (one manual approval).

This drives ONLY the vezir client side and prints the nostrconnect:// URI +
QR for you to scan in Amber on your phone.  It then waits for connect ->
get_public_key -> sign_event over the real relays, with full verbose relay
logging, and prints the signed event.  No server is involved (the handshake
is relay-mediated and server-independent); the server POST path is already
covered by scripts/nip46_full_login_test.py.

This isolates the exact thing only a phone can exercise: real Amber's
behavior on the wire.  Watch the [nip46] debug lines to see which relays
deliver each response.

Run:
    python scripts/nip46_amber_live.py
    # scan the QR in Amber, approve the connection, approve the signature.

Options:
    --relays ...   override relay set (default: vezir's blink-matched 5)
    --timeout S    seconds to wait for you to approve (default 180)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from vezir.client.nostr import event as nostr_event
from vezir.client.nostr import nip46


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--relays", nargs="*", default=None)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="[nip46] %(message)s")
    logging.getLogger("vezir.nip46").setLevel(logging.DEBUG)

    relays = args.relays if args.relays else nip46.DEFAULT_RELAYS

    def _on_auth(url: str) -> None:
        print(f"\n  >> Signer needs approval — open: {url}\n", flush=True)

    client = nip46.Nip46Client(relays=relays, name="vezir-live",
                               on_auth_url=_on_auth)
    uri = client.build_connect_uri()

    print("\n=== Scan this in Amber (or paste the URI) ===\n", flush=True)
    try:
        from vezir.server.enroll import render_qr_terminal
        print(render_qr_terminal(uri), flush=True)
    except Exception as exc:
        print(f"(QR render unavailable: {exc})", flush=True)
    print(uri, flush=True)
    print(f"\nRelays: {client.relays}", flush=True)
    print(f"\nWaiting up to {int(args.timeout)}s for Amber...\n", flush=True)

    t0 = time.time()
    try:
        user_pubkey = client.wait_for_connection(timeout=args.timeout)
        print(f"\n[OK] connected. user_pubkey={user_pubkey}", flush=True)
        print(f"[OK] remote_signer={client.remote_signer_pubkey}", flush=True)

        unsigned = {
            "kind": 27235,
            "created_at": int(time.time()),
            "tags": [["u", "https://example.invalid/api/auth/nostr/login"],
                     ["method", "POST"]],
            "content": "",
        }
        print("\nRequesting signature (approve in Amber)...", flush=True)
        signed = client.sign_event(unsigned, timeout=args.timeout)
        ok = nostr_event.verify_event(signed)
        print(f"\n[OK] signed event id={signed['id']}", flush=True)
        print(f"[OK] pubkey={signed['pubkey']}", flush=True)
        print(f"[OK] signature verifies: {ok}", flush=True)
        print(f"[OK] total handshake {time.time()-t0:.1f}s", flush=True)
        print("\nRESULT: PASS" if ok else "\nRESULT: FAIL (bad signature)", flush=True)
        return 0 if ok else 1
    except nip46.Nip46Error as exc:
        print(f"\nRESULT: FAIL — {exc}", flush=True)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
