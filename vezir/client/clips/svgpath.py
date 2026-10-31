"""SVG icons as libass vector drawings (the ticker's separator icon).

Only what a flat icon needs: the ``d`` of every ``<path>`` (M L H V C S Q T Z,
absolute and relative), no transforms, no arcs.  Anything else is rejected
when the kit is loaded, so a bad icon fails before a render starts rather
than drawing garbage into every clip.

An :class:`Icon` is normalized to height 1 with its top-left at the origin;
:meth:`Icon.drawing` scales it to pixels for an ASS ``{\\pN}`` block.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "Z": 0}
# ASS drawing precision: {\p3} divides coordinates by 2**(3-1) = 4.
DRAW_SCALE_LEVEL = 3


class IconError(ValueError):
    pass


Segment = tuple[str, tuple[float, ...]]  # ("m"|"l"|"b", absolute coordinates)


@dataclass(frozen=True)
class Icon:
    segments: tuple[Segment, ...]   # normalized: height 1, top-left at (0, 0)
    aspect: float                   # width / height

    def drawing(self, height: float) -> str:
        """ASS drawing commands for this icon at ``height`` pixels."""
        k = height * 2 ** (DRAW_SCALE_LEVEL - 1)
        return " ".join(
            f"{op} " + " ".join(str(round(v * k)) for v in coords)
            for op, coords in self.segments
        )


def parse_path(d: str) -> list[Segment]:
    """Absolute ``m``/``l``/``b`` (cubic) segments for an SVG path ``d``."""
    tokens = _TOKEN.findall(d)
    if "".join(tokens).replace(" ", "") != re.sub(r"[\s,]", "", d):
        raise IconError("path data has characters this parser does not understand")
    out: list[Segment] = []
    i, cmd = 0, ""
    x = y = sx = sy = 0.0
    last_ctrl: tuple[str, float, float] | None = None   # for S/T reflection

    def nums(n: int) -> list[float]:
        nonlocal i
        if i + n > len(tokens) or any(t.isalpha() for t in tokens[i:i + n]):
            raise IconError(f"path command {cmd!r} is missing numbers")
        vals = [float(t) for t in tokens[i:i + n]]
        i += n
        return vals

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd.upper() not in _ARGS:
                raise IconError(f"unsupported path command {cmd!r} (arcs and others)")
            if cmd in "Zz":
                if out:
                    out.append(("l", (sx, sy)))
                x, y = sx, sy
                last_ctrl = None
                continue
        elif not cmd or cmd in "Zz":
            raise IconError("path data must start with a command")
        rel = cmd.islower()
        up = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if up == "M":
            px, py = nums(2)
            x, y = ox + px, oy + py
            sx, sy = x, y
            out.append(("m", (x, y)))
            cmd = "l" if rel else "L"    # subsequent pairs are implicit linetos
            last_ctrl = None
        elif up in "LHV":
            if up == "L":
                px, py = nums(2)
                x, y = ox + px, oy + py
            elif up == "H":
                x = ox + nums(1)[0] if rel else nums(1)[0]
            else:
                y = oy + nums(1)[0] if rel else nums(1)[0]
            out.append(("l", (x, y)))
            last_ctrl = None
        elif up in "CS":
            if up == "C":
                x1, y1, x2, y2, px, py = nums(6)
                c1 = (ox + x1, oy + y1)
            else:
                x2, y2, px, py = nums(4)
                c1 = ((2 * x - last_ctrl[1], 2 * y - last_ctrl[2])
                      if last_ctrl and last_ctrl[0] == "C" else (x, y))
            c2 = (ox + x2, oy + y2)
            end = (ox + px, oy + py)
            out.append(("b", (*c1, *c2, *end)))
            x, y = end
            last_ctrl = ("C", *c2)
        else:  # Q / T: quadratic -> cubic
            if up == "Q":
                qx1, qy1, px, py = nums(4)
                q = (ox + qx1, oy + qy1)
            else:
                px, py = nums(2)
                q = ((2 * x - last_ctrl[1], 2 * y - last_ctrl[2])
                     if last_ctrl and last_ctrl[0] == "Q" else (x, y))
            end = (ox + px, oy + py)
            c1 = (x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y))
            c2 = (end[0] + 2 / 3 * (q[0] - end[0]), end[1] + 2 / 3 * (q[1] - end[1]))
            out.append(("b", (*c1, *c2, *end)))
            x, y = end
            last_ctrl = ("Q", *q)
    return out


def load(path: Path) -> Icon:
    """Read every ``<path d>`` of an SVG file into one normalized icon."""
    try:
        # A local kit file the user owns, not network input.
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise IconError(f"cannot read SVG {path}: {exc}") from None
    segments: list[Segment] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if el.get("transform"):
            raise IconError(f"{path}: transform attributes are not supported; flatten the icon")
        if tag == "path" and el.get("d"):
            segments += parse_path(el.get("d", ""))
    if not segments:
        raise IconError(f"{path}: no <path d=...> found")
    xs = [c[k] for _, c in segments for k in range(0, len(c), 2)]
    ys = [c[k] for _, c in segments for k in range(1, len(c), 2)]
    x0, y0 = min(xs), min(ys)
    w, h = max(xs) - x0, max(ys) - y0
    if w <= 0 or h <= 0:
        raise IconError(f"{path}: the icon has no area")
    norm = tuple(
        (op, tuple((v - (x0 if k % 2 == 0 else y0)) / h for k, v in enumerate(c)))
        for op, c in segments
    )
    return Icon(segments=norm, aspect=w / h)
