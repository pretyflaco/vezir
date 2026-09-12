#!/usr/bin/env python3
"""Read-only download/traffic stats across the Vezir ecosystem.

Pulls four public-ish sources and prints one consolidated table:

  1. PyPI recent (day/week/month) — pypistats.org JSON API, no auth.
  2. PyPI lifetime total          — the pepy.tech personalized-badge SVG.
     (The badge image endpoint is public; only api.pepy.tech needs a paid
     key.  We parse the number the badge already renders, e.g. "41k".)
  3. GitHub release-asset downloads — `gh api .../releases`, needs `gh`
     auth (push access not required for public repos).
  4. GitHub clone/view traffic (14-day rolling) — `gh api .../traffic/*`,
     needs *push* access.

Stdlib only.  Nothing is written; no network writes.  `--json` emits the
same data machine-readably.

Read the caveats printed at the bottom before quoting any number: PyPI
counts include CI, mirrors, and bots (our own release-verification curls,
the 3.10/3.11/3.12 CI matrix, and saray redeploys all land here), so for
an alpha they likely dwarf real installs.  GitHub release assets are ~0
for the Python packages by design — everyone installs via PyPI — so only
the vezir-android APK count is a true install proxy there.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# package (PyPI name) -> GitHub repo.  None PyPI name = APK-only (android).
TARGETS = [
    ("vezir", "vezir"),
    ("millet-pipeline", "millet"),
    ("millet-record", "millet-record"),
    (None, "vezir-android"),
]

OWNER = "pretyflaco"
PYPISTATS_DELAY = 3.0  # seconds between calls; the API 429s on rapid-fire calls
PYPISTATS_BACKOFF = 20.0  # seconds to wait before a single retry after a 429
UA = {"User-Agent": "vezir-download-stats (+https://github.com/pretyflaco/vezir)"}


def _get(url: str, timeout: int = 15) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return b"429"
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def pypi_recent(pkg: str) -> dict:
    """last_day / last_week / last_month, or an 'error' key.

    pypistats 429s aggressively; on a limit we back off once and retry.
    """
    url = f"https://pypistats.org/api/packages/{pkg}/recent"
    raw = _get(url)
    if raw == b"429":
        time.sleep(PYPISTATS_BACKOFF)
        raw = _get(url)
    if raw == b"429":
        return {"error": "rate-limited (retry in a minute)"}
    if raw is None:
        return {"error": "unreachable"}
    try:
        return json.loads(raw).get("data", {})
    except json.JSONDecodeError:
        return {"error": "bad response (likely rate-limited)"}


# The count is the last numeric <text> in the SVG; its textLength varies
# with digit width (210 for "41k", 130 for "8k"), so match on shape not width.
_BADGE_COUNT = re.compile(r'>([0-9][0-9.]*\s*[kKmMbB]?)</text>')


def pepy_total(pkg: str) -> str:
    """Lifetime total as the badge renders it (coarse: '41k', '1.2M')."""
    url = (
        f"https://static.pepy.tech/personalized-badge/{pkg}"
        "?period=total&units=INTERNATIONAL_SYSTEM"
        "&left_color=BLACK&right_color=GREEN&left_text=downloads"
    )
    raw = _get(url)
    if not raw or raw == b"429":
        return "?"
    # The value is rendered twice (shadow + face); both are the last two
    # numeric matches, so either tail element is the count.
    matches = _BADGE_COUNT.findall(raw.decode("utf-8", "replace"))
    return matches[-1].strip() if matches else "?"


def _gh_json(path: str, paginate: bool = False) -> object | None:
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        ).stdout
    except FileNotFoundError:
        return "NO_GH"
    except subprocess.CalledProcessError:
        return None
    # --paginate concatenates JSON arrays as "][", stitch them back.
    if paginate and "][" in out:
        out = out.replace("][", ",")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def gh_release_downloads(repo: str) -> dict:
    """Summed asset downloads, plus APK-only subtotal for android."""
    data = _gh_json(f"repos/{OWNER}/{repo}/releases", paginate=True)
    if data == "NO_GH":
        return {"error": "gh not installed"}
    if data is None:
        return {"error": "unreachable / no releases"}
    total = 0
    apk = 0
    for rel in data:
        for asset in rel.get("assets", []) or []:
            n = asset.get("download_count", 0)
            total += n
            if asset.get("name", "").endswith(".apk"):
                apk += n
    result = {"asset_total": total}
    if apk:
        result["apk_total"] = apk
    return result


def gh_traffic(repo: str) -> dict:
    """14-day rolling clones + views (count/uniques).  Push access only."""
    out: dict = {}
    for kind in ("clones", "views"):
        data = _gh_json(f"repos/{OWNER}/{repo}/traffic/{kind}")
        if data == "NO_GH":
            return {"error": "gh not installed"}
        if data is None:
            out[kind] = "n/a (needs push access)"
        else:
            out[kind] = {"count": data.get("count"), "uniques": data.get("uniques")}
    return out


def collect() -> list[dict]:
    rows = []
    first_pypi = True
    for pkg, repo in TARGETS:
        row: dict = {"repo": repo, "pypi_package": pkg}
        if pkg:
            if not first_pypi:
                time.sleep(PYPISTATS_DELAY)  # be polite to pypistats
            first_pypi = False
            row["pypi_recent"] = pypi_recent(pkg)
            row["pepy_total"] = pepy_total(pkg)
        row["gh_releases"] = gh_release_downloads(repo)
        row["gh_traffic"] = gh_traffic(repo)
        rows.append(row)
    return rows


def _fmt_recent(r: dict) -> str:
    if "error" in r:
        return r["error"]
    return f"{r.get('last_day', '?')}/{r.get('last_week', '?')}/{r.get('last_month', '?')}"


def _fmt_releases(r: dict) -> str:
    if "error" in r:
        return r["error"]
    s = str(r.get("asset_total", 0))
    if "apk_total" in r:
        s += f" ({r['apk_total']} APK)"
    return s


def _fmt_traffic(t: dict) -> str:
    if "error" in t:
        return t["error"]
    parts = []
    for kind in ("clones", "views"):
        v = t.get(kind)
        if isinstance(v, dict):
            parts.append(f"{kind[0]}={v['count']}/{v['uniques']}u")
        else:
            parts.append(f"{kind[0]}={v}")
    return " ".join(parts)


def print_table(rows: list[dict]) -> None:
    print()
    print(f"{'repo':<15} {'pypi d/w/m':<22} {'total':<7} {'gh assets':<16} {'traffic 14d'}")
    print("-" * 90)
    for row in rows:
        recent = _fmt_recent(row["pypi_recent"]) if row["pypi_package"] else "— (APK only)"
        total = row.get("pepy_total", "—") if row["pypi_package"] else "—"
        print(
            f"{row['repo']:<15} {recent:<22} {total:<7} "
            f"{_fmt_releases(row['gh_releases']):<16} {_fmt_traffic(row['gh_traffic'])}"
        )
    print()
    print("Caveats:")
    print("  - pypi d/w/m = downloads last day/week/month; includes CI,")
    print("    mirrors, bots, our own release curls + saray redeploys.")
    print("  - total = pepy.tech lifetime, parsed from the badge (coarse).")
    print("  - gh assets ~0 for Python pkgs by design (PyPI is the channel);")
    print("    only the vezir-android APK count proxies real installs.")
    print("  - traffic = 14-day rolling clones/views (c/v count/uniques),")
    print("    needs push access or shows n/a.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()
    rows = collect()
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
