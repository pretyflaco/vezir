#!/usr/bin/env python3
"""Layer 2: full local end-to-end vezir login, signed by the harness.

Stands up a REAL vezir server (uvicorn on localhost, isolated VEZIR_DATA),
allowlists the harness signer's pubkey, runs the NIP-46 handshake over REAL
relays against the Amber-emulating harness, then POSTs the signed NIP-98
login event to the server's /api/auth/nostr/login and checks a session JWT
comes back and works on /api/me.

This covers the complete path minus the phone: client multi-relay NIP-46 ->
server BIP-340 verification -> JWT mint -> bearer auth.

Run:  python scripts/nip46_full_login_test.py [--scheme nip04 --no-echo-id --reply-relays 1]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time

from vezir.client.nostr import nip46

sys.path.insert(0, "scripts")
from nip46_signer_harness import SignerHarness


def _log(msg: str) -> None:
    print(f"[full] {msg}", file=sys.stderr, flush=True)


def _start_server(port: int):
    """Start uvicorn in a background thread; return (thread, base_url)."""
    import uvicorn

    from vezir.server.app import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for startup.
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    return server, f"http://127.0.0.1:{port}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--relays", nargs="*", default=None)
    ap.add_argument("--scheme", choices=["nip04", "nip44"], default="nip44")
    ap.add_argument("--no-echo-id", action="store_true")
    ap.add_argument("--reply-relays", type=int, default=None)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()

    relays = args.relays if args.relays else nip46.DEFAULT_RELAYS

    # Isolate runtime state in a temp dir so we never touch ~/vezir-data.
    tmp = tempfile.mkdtemp(prefix="vezir-nip46-e2e-")
    os.environ["VEZIR_DATA"] = tmp
    os.environ.setdefault("VEZIR_JWT_SECRET", "test-secret-not-for-prod")
    _log(f"VEZIR_DATA={tmp}")

    _server, base_url = _start_server(args.port)
    _log(f"server up at {base_url}")

    try:
        # Build the client first so we know the secret/URI; harness derives
        # its own pubkey, which we must allowlist before the POST.
        client = nip46.Nip46Client(relays=relays, name="vezir-e2e")
        uri = client.build_connect_uri()
        harness = SignerHarness(
            uri,
            scheme=args.scheme,
            echo_id=not args.no_echo_id,
            reply_relays=args.reply_relays,
        )

        # Allowlist the signer's pubkey (what the signed event will carry).
        from vezir.server import nostr_members
        nostr_members.add(harness.pubkey, "loopback-tester", label="harness")
        _log(f"allowlisted signer pubkey {harness.pubkey[:16]}…")

        threading.Thread(target=lambda: harness.run(timeout=args.timeout),
                         daemon=True).start()
        time.sleep(2.0)

        # NIP-46 handshake over real relays.
        client.wait_for_connection(timeout=args.timeout)
        from vezir.client.nostr import login as nostr_login
        template = nostr_login.build_login_event_template(
            nostr_login.login_url_for(base_url)
        )
        signed = client.sign_event(template, timeout=args.timeout)
        _log(f"signed login event pubkey={signed['pubkey'][:16]}…")

        # POST through the real HTTP path.
        body = nostr_login.post_login(
            base_url, nostr_login.auth_header_from_event(signed), verify=False
        )
        jwt = body.get("session_jwt")
        npub = body.get("npub")
        gh = body.get("github")
        _log(f"login OK: github={gh} npub={(npub or '')[:16]}… jwt_len={len(jwt or '')}")

        # Use the JWT on /api/me.
        import httpx
        me = httpx.get(f"{base_url}/api/me",
                       headers={"Authorization": f"Bearer {jwt}"}, timeout=10)
        _log(f"/api/me -> {me.status_code} {me.json() if me.status_code==200 else me.text[:200]}")

        ok = (me.status_code == 200 and me.json().get("github") == "loopback-tester")
        print({"result": "PASS" if ok else "FAIL"})
        return 0 if ok else 1
    except Exception as exc:
        _log(f"FAIL: {exc!r}")
        print({"result": "FAIL"})
        return 2
    finally:
        try:
            client.close()
            harness.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
