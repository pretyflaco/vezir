#!/usr/bin/env python3
"""Drive vezir's Nip46Client against the local signer harness over REAL relays.

This is the automated, phone-free validation of the multi-relay NIP-46
login handshake — the exact path that was hanging after connect.  It runs
the vezir CLIENT and the Amber-emulating SIGNER (scripts/nip46_signer_harness.py)
in two threads, both talking to live public relays.

It exercises, over real websockets:
  * multi-relay fan-out of the nostrconnect:// URI,
  * connect ack handling,
  * get_public_key round-trip,
  * sign_event round-trip + local verification,
  * (optionally) Amber quirks: nip04 encryption, non-echoed UUID ids, and
    responses landing on only a SUBSET of the advertised relays (the
    redundancy stress test that single-relay vezir failed).

Exit code 0 = full handshake + signature verified.

Examples::

    # Baseline (nip44, echoed ids, all relays):
    python scripts/nip46_loopback_test.py

    # Full Amber emulation incl. single-relay reply (redundancy proof):
    python scripts/nip46_loopback_test.py --scheme nip04 --no-echo-id --reply-relays 1

    # Use only a couple of fast relays for speed:
    python scripts/nip46_loopback_test.py --relays wss://relay.damus.io wss://nos.lol
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from vezir.client.nostr import nip46

sys.path.insert(0, "scripts")
from nip46_signer_harness import SignerHarness


def _client_log(msg: str) -> None:
    print(f"[client] {msg}", file=sys.stderr, flush=True)


def run(
    relays: list[str],
    *,
    scheme: str,
    echo_id: bool,
    reply_relays: int | None,
    listen_relays: int | None,
    connect_delay: float,
    timeout: float,
) -> int:
    client = nip46.Nip46Client(relays=relays, name="vezir-loopback")
    uri = client.build_connect_uri()
    _client_log(f"URI relays: {client.relays}")
    _client_log(f"client_pubkey: {client.client_pubkey}")

    harness = SignerHarness(
        uri,
        scheme=scheme,
        echo_id=echo_id,
        reply_relays=reply_relays,
        listen_relays=listen_relays,
        connect_delay=connect_delay,
    )

    result: dict = {}

    def _run_signer() -> None:
        try:
            harness.run(timeout=timeout)
        except Exception as exc:  # pragma: no cover
            result["signer_error"] = repr(exc)

    signer_thread = threading.Thread(target=_run_signer, daemon=True)
    signer_thread.start()
    # Give the signer a moment to subscribe before the client connects.
    time.sleep(2.0)

    t0 = time.time()
    try:
        user_pubkey = client.wait_for_connection(timeout=timeout)
        _client_log(f"connected; user_pubkey={user_pubkey} "
                    f"(remote_signer={client.remote_signer_pubkey})")

        unsigned = {
            "kind": 27235,
            "created_at": int(time.time()),
            "tags": [["u", "https://vezir.example/api/auth/nostr/login"],
                     ["method", "POST"]],
            "content": "",
        }
        signed = client.sign_event(unsigned, timeout=timeout)
        elapsed = time.time() - t0
        _client_log(f"signed event id={signed['id'][:16]}… pubkey={signed['pubkey'][:16]}…")
        _client_log(f"handshake completed in {elapsed:.1f}s")

        # Assertions.
        ok = True
        if signed["pubkey"] != harness.pubkey:
            _client_log(f"FAIL: signed pubkey {signed['pubkey']} != signer {harness.pubkey}")
            ok = False
        from vezir.client.nostr import event as nostr_event
        if not nostr_event.verify_event(signed):
            _client_log("FAIL: signature did not verify")
            ok = False
        if client.user_pubkey != harness.pubkey:
            _client_log(f"WARN: user_pubkey {client.user_pubkey} != signer "
                        f"{harness.pubkey} (get_public_key may have fallen back)")
        return 0 if ok else 1
    except nip46.Nip46Error as exc:
        _client_log(f"FAIL: {exc}")
        if result.get("signer_error"):
            _client_log(f"signer_error: {result['signer_error']}")
        return 2
    finally:
        client.close()
        harness.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--relays", nargs="*", default=None,
                    help="relay URLs (default: vezir DEFAULT_RELAYS)")
    ap.add_argument("--scheme", choices=["nip04", "nip44"], default="nip44")
    ap.add_argument("--no-echo-id", action="store_true")
    ap.add_argument("--reply-relays", type=int, default=None,
                    help="signer replies on only the first N relays")
    ap.add_argument("--listen-relays", type=int, default=None,
                    help="signer reads the client's requests on only the "
                         "first N relays (simulates the laptop overlap gap)")
    ap.add_argument("--connect-delay", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                            format="[nip46] %(message)s")
        logging.getLogger("vezir.nip46").setLevel(logging.DEBUG)

    relays = args.relays if args.relays else nip46.DEFAULT_RELAYS
    rc = run(
        relays,
        scheme=args.scheme,
        echo_id=not args.no_echo_id,
        reply_relays=args.reply_relays,
        listen_relays=args.listen_relays,
        connect_delay=args.connect_delay,
        timeout=args.timeout,
    )
    print(json.dumps({"result": "PASS" if rc == 0 else "FAIL", "code": rc}))
    sys.exit(rc)


if __name__ == "__main__":
    main()
