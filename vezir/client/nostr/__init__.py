"""Client-side nostr primitives for `vezir login` (NIP-46 remote signing).

Modules:
  * ``nip44`` — NIP-44 v2 authenticated encryption (the wire crypto for
    NIP-46 request/response events).  Validated against the official
    nip44 test vectors.
  * ``event`` — minimal nostr event id/signature helpers (coincurve
    Schnorr) used to build the NIP-98 login event and the NIP-46
    request events.
  * ``nip46`` — the ``nostrconnect://`` remote-signer client.

These live under the TUI client and pull ``coincurve`` + ``cryptography``
+ ``websocket-client``; they are imported lazily by ``vezir login`` so a
plain ``pip install vezir`` (no ``[tui]``) never needs them.
"""
