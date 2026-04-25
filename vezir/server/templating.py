"""Shared Jinja2 templates instance."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
