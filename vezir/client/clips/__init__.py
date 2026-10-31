"""Branded clip rendering (v0.29.0): `vezir clips render`.

Turns a ``video-clipping`` session's cut list (``clips.json``) into
shareable social-media clips.  The renderer is brand-agnostic: everything
brand- or purpose-specific is data supplied by the user.

* **Brand kit** (a directory with ``kit.toml``): what it looks like.
  Colors, font, logos, background pattern, glossary, optional designer
  motion files for the cards.
* **Campaign** (a ``.toml`` file): what it promotes.  Ticker items, end
  card, link, hashtags, handles, glossary.
* **Profile** (built in, :mod:`.profiles`): where it is posted.  Canvas,
  layout boxes, platform safe zones, card lengths, caption style.

Nothing here talks to the vezir server except fetching the cut list and,
when no local copy is given, streaming the source video.
"""
from __future__ import annotations
