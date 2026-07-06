"""Vezir — internal scribe service wrapping millet."""

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("vezir")
except Exception:
    __version__ = "0.11.0"
