"""Per-team custom vocabulary / glossary store and API routes.

Stores each team's special words, known ASR mishearing aliases, and short
glossary notes at ``~/vezir-data/teams/<team_id>/vocabulary.json`` (0600).
The worker exposes this file to millet through the per-job HOME shim so
ASR decoding (initial_prompt/hotwords), post-ASR alias cleanup, and
summary generation all recognize team-specific terminology.

Routes (all team-scoped via ``X-Team-Id``; any team member may manage their
own team's vocabulary):

    GET    /api/vocabulary                 get team vocabulary
    PUT    /api/vocabulary                 replace team vocabulary
    POST   /api/vocabulary/terms           add or update a single term
    DELETE /api/vocabulary/terms/{term}    remove a term
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import config
from . import auth, ratelimit

log = logging.getLogger("vezir.vocabulary")
router = APIRouter()

MAX_TERMS = 500
MAX_ALIASES_PER_TERM = 50
MAX_TERM_LEN = 100
MAX_DESC_LEN = 300
MAX_HARVEST_SESSIONS = 200
MAX_HARVEST_ANCHORS = 120
MAX_REJECTED_PAIRS = 2000

# Reject control characters (including newlines) so glossary entries cannot
# smuggle multi-line prompt instructions into LLM prompts.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_VOCAB_LOCK = threading.Lock()


def _empty_vocab() -> dict[str, Any]:
    return {"terms": [], "aliases": {}, "glossary": {}}


def _validate_piece(value: str, *, field: str, max_len: int) -> str:
    if _CTRL_RE.search(value):
        raise ValueError(f"{field} must not contain control characters or newlines")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError(f"{field} must be non-empty")
    if len(cleaned) > max_len:
        raise ValueError(f"{field} exceeds maximum length of {max_len} characters")
    return cleaned


def normalize_vocabulary(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a vocabulary payload."""
    terms: list[str] = []
    seen_terms: set[str] = set()

    raw_terms = data.get("terms", [])
    if not isinstance(raw_terms, list):
        raise ValueError("terms must be a list of strings")
    for item in raw_terms:
        if not isinstance(item, str):
            raise ValueError("each term must be a string")
        cleaned = _validate_piece(item, field="term", max_len=MAX_TERM_LEN)
        key = cleaned.casefold()
        if key not in seen_terms:
            seen_terms.add(key)
            terms.append(cleaned)

    aliases: dict[str, list[str]] = {}
    alias_keys: dict[str, str] = {}
    raw_aliases = data.get("aliases", {})
    if not isinstance(raw_aliases, Mapping):
        raise ValueError("aliases must be an object mapping canonical terms to lists")
    for raw_canon, raw_variants in raw_aliases.items():
        if not isinstance(raw_canon, str):
            raise ValueError("alias canonical key must be a string")
        canon = _validate_piece(raw_canon, field="alias key", max_len=MAX_TERM_LEN)
        if not isinstance(raw_variants, list):
            raise ValueError(f"aliases for {canon!r} must be a list of strings")
        if len(raw_variants) > MAX_ALIASES_PER_TERM:
            raise ValueError(
                f"aliases for {canon!r} exceed maximum of {MAX_ALIASES_PER_TERM}"
            )
        canon_key = canon.casefold()
        target = alias_keys.setdefault(canon_key, canon)
        bucket = aliases.setdefault(target, [])
        seen_v = {x.casefold() for x in bucket} | {target.casefold()}
        for v in raw_variants:
            if not isinstance(v, str):
                raise ValueError(f"each alias for {canon!r} must be a string")
            cv = _validate_piece(v, field="alias", max_len=MAX_TERM_LEN)
            if cv.casefold() not in seen_v:
                seen_v.add(cv.casefold())
                bucket.append(cv)
        if not bucket:
            aliases.pop(target, None)

    # Reject alias/canonical chains and cycles case-insensitively (e.g.
    # `a -> B` and `B -> C`, or `A <-> B`), and reject one alias claiming two
    # different canonical targets so repeated vocabulary application is always
    # deterministic and idempotent.
    active_canon_keys = {k.casefold(): k for k in aliases}
    claimed_aliases: dict[str, str] = {}
    for canon, variants in aliases.items():
        for v in variants:
            v_key = v.casefold()
            if v_key in active_canon_keys:
                raise ValueError(
                    f"alias {v!r} for {canon!r} is also a canonical term "
                    f"({active_canon_keys[v_key]!r}); chained or cyclic aliases are not allowed"
                )
            prev = claimed_aliases.get(v_key)
            if prev is not None and prev != canon:
                raise ValueError(
                    f"alias {v!r} cannot map to both {prev!r} and {canon!r}"
                )
            claimed_aliases[v_key] = canon

    glossary: dict[str, str] = {}
    glossary_keys: dict[str, str] = {}
    raw_glossary = data.get("glossary", {})
    if not isinstance(raw_glossary, Mapping):
        raise ValueError("glossary must be an object mapping terms to descriptions")
    for raw_term, raw_desc in raw_glossary.items():
        if not isinstance(raw_term, str) or not isinstance(raw_desc, str):
            raise ValueError("glossary keys and descriptions must be strings")
        term = _validate_piece(raw_term, field="glossary term", max_len=MAX_TERM_LEN)
        desc = _validate_piece(raw_desc, field="glossary description", max_len=MAX_DESC_LEN)
        key = term.casefold()
        canonical_key = glossary_keys.setdefault(key, term)
        glossary[canonical_key] = desc

    # Ensure total distinct canonical terms stays bounded.
    distinct = (
        {t.casefold() for t in terms}
        | {k.casefold() for k in aliases}
        | {k.casefold() for k in glossary}
    )
    if len(distinct) > MAX_TERMS:
        raise ValueError(f"vocabulary exceeds maximum of {MAX_TERMS} distinct terms")

    return {"terms": terms, "aliases": aliases, "glossary": glossary}


def load_team_vocabulary(team_id: str) -> dict[str, Any]:
    """Load a team's vocabulary from disk, returning an empty template if unset."""
    path = config.team_vocabulary_path(team_id)
    if not path.is_file():
        return _empty_vocab()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            return _empty_vocab()
        return normalize_vocabulary(raw)
    except Exception as exc:
        log.warning("team %s: could not read vocabulary.json (%s)", team_id, exc)
        return _empty_vocab()


def save_team_vocabulary(team_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist a team's vocabulary (0600)."""
    normalized = normalize_vocabulary(data)
    path = config.team_vocabulary_path(team_id)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    with _VOCAB_LOCK:
        config.secure_mkdir(path.parent)
        config.secure_write_text(path, payload)
    return normalized


def add_team_term(
    team_id: str,
    term: str,
    *,
    aliases: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Add or update a term (with optional aliases and glossary note)."""
    cleaned_term = _validate_piece(term, field="term", max_len=MAX_TERM_LEN)
    term_key = cleaned_term.casefold()

    with _VOCAB_LOCK:
        current = load_team_vocabulary(team_id)
        terms: list[str] = list(current["terms"])
        existing_idx = next(
            (i for i, t in enumerate(terms) if t.casefold() == term_key), None
        )
        if existing_idx is None:
            terms.append(cleaned_term)
        else:
            # Update canonical casing to the caller's latest spelling.
            terms[existing_idx] = cleaned_term

        alias_map: dict[str, list[str]] = {
            k: list(v) for k, v in current["aliases"].items()
        }
        if aliases:
            prev_key = next(
                (k for k in alias_map if k.casefold() == term_key), None
            )
            existing_aliases = alias_map.pop(prev_key, []) if prev_key else []
            merged_aliases = existing_aliases + list(aliases)
            alias_map[cleaned_term] = merged_aliases
        else:
            # Keep canonical key casing aligned if term casing changed.
            prev_key = next(
                (k for k in alias_map if k.casefold() == term_key), None
            )
            if prev_key and prev_key != cleaned_term:
                alias_map[cleaned_term] = alias_map.pop(prev_key)

        glossary_map: dict[str, str] = dict(current["glossary"])
        if description is not None:
            prev_gkey = next(
                (k for k in glossary_map if k.casefold() == term_key), None
            )
            if prev_gkey:
                glossary_map.pop(prev_gkey)
            if description.strip():
                glossary_map[cleaned_term] = description
        else:
            prev_gkey = next(
                (k for k in glossary_map if k.casefold() == term_key), None
            )
            if prev_gkey and prev_gkey != cleaned_term:
                glossary_map[cleaned_term] = glossary_map.pop(prev_gkey)

        normalized = normalize_vocabulary(
            {"terms": terms, "aliases": alias_map, "glossary": glossary_map}
        )
        path = config.team_vocabulary_path(team_id)
        payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
        config.secure_mkdir(path.parent)
        config.secure_write_text(path, payload)
        return normalized


def remove_team_term(team_id: str, term: str) -> tuple[dict[str, Any], bool]:
    """Remove *term* (case-insensitively) from terms, aliases, and glossary."""
    cleaned = _validate_piece(term, field="term", max_len=MAX_TERM_LEN)
    key = cleaned.casefold()

    with _VOCAB_LOCK:
        current = load_team_vocabulary(team_id)
        terms = [t for t in current["terms"] if t.casefold() != key]
        aliases = {
            k: v for k, v in current["aliases"].items() if k.casefold() != key
        }
        glossary = {
            k: v for k, v in current["glossary"].items() if k.casefold() != key
        }
        removed = (
            len(terms) != len(current["terms"])
            or len(aliases) != len(current["aliases"])
            or len(glossary) != len(current["glossary"])
        )
        if not removed:
            return current, False
        normalized = normalize_vocabulary(
            {"terms": terms, "aliases": aliases, "glossary": glossary}
        )
        path = config.team_vocabulary_path(team_id)
        payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
        config.secure_mkdir(path.parent)
        config.secure_write_text(path, payload)
        return normalized, True


# ── FastAPI routes ──────────────────────────────────────────────────────────


class _VocabularyPutBody(BaseModel):
    terms: list[str] = []
    aliases: dict[str, list[str]] = {}
    glossary: dict[str, str] = {}


class _AddTermBody(BaseModel):
    term: str
    aliases: list[str] | None = None
    description: str | None = None


@router.get("/api/vocabulary", dependencies=[Depends(ratelimit.limit_api)])
def get_vocabulary(
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Return the active team's vocabulary (terms, aliases, glossary)."""
    _github, team_id, _is_admin = auth_triple
    vocab = load_team_vocabulary(team_id)
    return {"team_id": team_id, **vocab}


@router.put("/api/vocabulary", dependencies=[Depends(ratelimit.limit_api)])
def put_vocabulary(
    body: _VocabularyPutBody = Body(...),
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Replace the active team's vocabulary."""
    github, team_id, _is_admin = auth_triple
    try:
        saved = save_team_vocabulary(
            team_id,
            {
                "terms": body.terms,
                "aliases": body.aliases,
                "glossary": body.glossary,
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info(
        "team=%s vocabulary replaced by %s (%d terms, %d alias groups, %d glossary)",
        team_id,
        github,
        len(saved["terms"]),
        len(saved["aliases"]),
        len(saved["glossary"]),
    )
    return {"team_id": team_id, **saved}


@router.post("/api/vocabulary/terms", dependencies=[Depends(ratelimit.limit_api)])
def post_vocabulary_term(
    body: _AddTermBody = Body(...),
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Add or merge a single term with optional aliases and glossary description."""
    github, team_id, _is_admin = auth_triple
    try:
        saved = add_team_term(
            team_id,
            body.term,
            aliases=body.aliases,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("team=%s vocabulary term %r updated by %s", team_id, body.term, github)
    return {"team_id": team_id, **saved}


@router.delete(
    "/api/vocabulary/terms/{term}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def delete_vocabulary_term(
    term: str,
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Remove a term (and its aliases/glossary entry) from the active team."""
    github, team_id, _is_admin = auth_triple
    try:
        saved, removed = remove_team_term(team_id, term)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not removed:
        raise HTTPException(404, f"term {term!r} not found in team vocabulary")
    log.info("team=%s vocabulary term %r removed by %s", team_id, term, github)
    return {"team_id": team_id, "removed": True, **saved}


# ── Harvest state, rejection memory, and human-reviewed decision routes ──────


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_team_rejected(team_id: str) -> list[dict[str, str]]:
    """Load per-team dismissed ``(canonical, observed)`` harvest pairs."""
    path = config.team_vocabulary_rejected_path(team_id)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            return []
        entries = raw.get("rejected")
        if not isinstance(entries, list):
            return []
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            canon = str(item.get("canonical") or "").strip()
            obs = str(item.get("observed") or "").strip()
            if not canon or not obs:
                continue
            key = (canon.casefold(), obs.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "canonical": canon,
                    "observed": obs,
                    "rejected_at": str(item.get("rejected_at") or ""),
                    "rejected_by": str(item.get("rejected_by") or ""),
                }
            )
        return out
    except Exception as exc:
        log.warning("team %s: could not read vocabulary_rejected.json (%s)", team_id, exc)
        return []


def _write_team_rejected_locked(team_id: str, entries: list[dict[str, str]]) -> None:
    path = config.team_vocabulary_rejected_path(team_id)
    payload = json.dumps({"rejected": entries}, indent=2, ensure_ascii=False) + "\n"
    config.secure_mkdir(path.parent)
    config.secure_write_text(path, payload)


def remove_team_rejection(
    team_id: str,
    *,
    canonical: str | None = None,
    observed: str | None = None,
    clear_all: bool = False,
) -> tuple[list[dict[str, str]], int]:
    """Remove one rejected pair (or clear all) for *team_id*."""
    with _VOCAB_LOCK:
        current = load_team_rejected(team_id)
        if clear_all:
            removed = len(current)
            if removed:
                _write_team_rejected_locked(team_id, [])
            return [], removed
        if not canonical or not observed:
            raise ValueError("canonical and observed are required unless clear_all=true")
        c_key = _validate_piece(canonical, field="canonical", max_len=MAX_TERM_LEN).casefold()
        o_key = _validate_piece(observed, field="observed", max_len=MAX_TERM_LEN).casefold()
        kept = [
            e
            for e in current
            if (e["canonical"].casefold(), e["observed"].casefold()) != (c_key, o_key)
        ]
        removed = len(current) - len(kept)
        if removed:
            _write_team_rejected_locked(team_id, kept)
        return kept, removed


def load_team_harvest_state(team_id: str) -> dict[str, Any]:
    """Load the latest harvest status & cached report for *team_id*."""
    path = config.team_vocabulary_harvest_path(team_id)
    if not path.is_file():
        return {"status": "idle", "error": None, "updated_at": None, "report": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            return {"status": "idle", "error": None, "updated_at": None, "report": None}
        return {
            "status": str(raw.get("status") or "idle"),
            "error": str(raw["error"]) if raw.get("error") is not None else None,
            "updated_at": str(raw["updated_at"]) if raw.get("updated_at") is not None else None,
            "report": raw.get("report") if isinstance(raw.get("report"), Mapping) else None,
        }
    except Exception as exc:
        log.warning("team %s: could not read vocabulary_harvest.json (%s)", team_id, exc)
        return {"status": "idle", "error": None, "updated_at": None, "report": None}


def save_team_harvest_state(team_id: str, state: Mapping[str, Any]) -> None:
    """Atomically persist per-team harvest state/report (0600)."""
    path = config.team_vocabulary_harvest_path(team_id)
    payload = json.dumps(dict(state), indent=2, ensure_ascii=False) + "\n"
    with _VOCAB_LOCK:
        config.secure_mkdir(path.parent)
        config.secure_write_text(path, payload)


def filter_pending_candidates(
    report: Mapping[str, Any] | None,
    vocab: Mapping[str, Any],
    rejected: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Return report candidates not yet approved or rejected."""
    if not report:
        return []
    raw_cands = report.get("candidates")
    if not isinstance(raw_cands, list):
        return []

    existing_aliases: set[tuple[str, str]] = set()
    aliases_map = vocab.get("aliases")
    if isinstance(aliases_map, Mapping):
        for canon, variants in aliases_map.items():
            if isinstance(variants, list):
                for v in variants:
                    existing_aliases.add((str(canon).casefold(), str(v).casefold()))

    rejected_keys: set[tuple[str, str]] = {
        (e["canonical"].casefold(), e["observed"].casefold()) for e in rejected
    }

    pending: list[dict[str, Any]] = []
    for item in raw_cands:
        if not isinstance(item, Mapping):
            continue
        canon = str(item.get("canonical") or "").strip()
        obs = str(item.get("observed") or "").strip()
        if not canon or not obs:
            continue
        key = (canon.casefold(), obs.casefold())
        if key in existing_aliases or key in rejected_keys:
            continue
        pending.append(dict(item))
    return pending


def apply_harvest_decisions(
    team_id: str,
    *,
    approve: list[dict[str, Any]],
    reject: list[dict[str, Any]],
    decided_by: str,
) -> dict[str, Any]:
    """Atomically apply batch approve/reject harvest decisions for *team_id*.

    Validates the full resulting vocabulary (limits, chains/cycles, control
    chars) BEFORE writing either ``vocabulary.json`` or
    ``vocabulary_rejected.json`` so invalid batches leave disk untouched.
    """
    with _VOCAB_LOCK:
        current = load_team_vocabulary(team_id)
        terms: list[str] = list(current["terms"])
        aliases: dict[str, list[str]] = {
            k: list(v) for k, v in current["aliases"].items()
        }
        glossary: dict[str, str] = dict(current["glossary"])

        for item in approve:
            canon = _validate_piece(
                str(item.get("canonical") or ""), field="canonical", max_len=MAX_TERM_LEN
            )
            layer = str(item.get("layer") or "alias").strip().lower()
            if layer not in ("alias", "term", "glossary"):
                raise ValueError(
                    f"invalid approval layer {layer!r} (expected 'alias', 'term', or 'glossary')"
                )

            c_key = canon.casefold()
            existing_term_idx = next(
                (i for i, t in enumerate(terms) if t.casefold() == c_key), None
            )
            if existing_term_idx is None:
                terms.append(canon)
            else:
                terms[existing_term_idx] = canon

            if layer == "alias":
                obs = _validate_piece(
                    str(item.get("observed") or ""), field="observed", max_len=MAX_TERM_LEN
                )
                prev_key = next((k for k in aliases if k.casefold() == c_key), None)
                bucket = aliases.pop(prev_key, []) if prev_key else []
                bucket.append(obs)
                aliases[canon] = bucket

            desc_raw = item.get("description")
            if desc_raw is not None and str(desc_raw).strip():
                desc = _validate_piece(
                    str(desc_raw), field="glossary description", max_len=MAX_DESC_LEN
                )
                prev_gkey = next((k for k in glossary if k.casefold() == c_key), None)
                if prev_gkey:
                    glossary.pop(prev_gkey)
                glossary[canon] = desc
            elif layer == "glossary":
                raise ValueError(
                    f"glossary approval for {canon!r} requires a non-empty description"
                )

        # Validate complete vocabulary BEFORE writing anything.
        normalized = normalize_vocabulary(
            {"terms": terms, "aliases": aliases, "glossary": glossary}
        )

        # Validate and build updated rejection list.
        rejected_entries = load_team_rejected(team_id)
        seen_rej = {
            (e["canonical"].casefold(), e["observed"].casefold()) for e in rejected_entries
        }
        now_str = _now_utc_iso()
        added_rejects = 0
        for item in reject:
            canon = _validate_piece(
                str(item.get("canonical") or ""), field="canonical", max_len=MAX_TERM_LEN
            )
            obs = _validate_piece(
                str(item.get("observed") or ""), field="observed", max_len=MAX_TERM_LEN
            )
            key = (canon.casefold(), obs.casefold())
            if key in seen_rej:
                continue
            seen_rej.add(key)
            rejected_entries.append(
                {
                    "canonical": canon,
                    "observed": obs,
                    "rejected_at": now_str,
                    "rejected_by": decided_by,
                }
            )
            added_rejects += 1
        if len(rejected_entries) > MAX_REJECTED_PAIRS:
            raise ValueError(
                f"rejected vocabulary list exceeds maximum of {MAX_REJECTED_PAIRS} entries"
            )

        # Persist atomically after all validations pass.
        if approve:
            vpath = config.team_vocabulary_path(team_id)
            config.secure_mkdir(vpath.parent)
            config.secure_write_text(
                vpath, json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
            )
        if added_rejects:
            _write_team_rejected_locked(team_id, rejected_entries)

        # Remove decided non-alias approvals from the cached harvest report so
        # reviewing a candidate as term/glossary-only clears it from pending.
        decided_keys: set[tuple[str, str]] = set()
        for item in approve:
            c_str = str(item.get("canonical") or "").strip()
            o_str = str(item.get("observed") or "").strip()
            if c_str and o_str:
                decided_keys.add((c_str.casefold(), o_str.casefold()))

        state = load_team_harvest_state(team_id)
        report_obj = state.get("report")
        if decided_keys and isinstance(report_obj, Mapping):
            raw_cands = report_obj.get("candidates")
            if isinstance(raw_cands, list):
                filtered_cands = [
                    c
                    for c in raw_cands
                    if not (
                        isinstance(c, Mapping)
                        and (
                            str(c.get("canonical") or "").strip().casefold(),
                            str(c.get("observed") or "").strip().casefold(),
                        )
                        in decided_keys
                    )
                ]
                if len(filtered_cands) != len(raw_cands):
                    updated_report = dict(report_obj)
                    updated_report["candidates"] = filtered_cands
                    state["report"] = updated_report
                    hpath = config.team_vocabulary_harvest_path(team_id)
                    config.secure_mkdir(hpath.parent)
                    config.secure_write_text(
                        hpath, json.dumps(state, indent=2, ensure_ascii=False) + "\n"
                    )

        pending = filter_pending_candidates(state.get("report"), normalized, rejected_entries)
        return {
            "team_id": team_id,
            "vocabulary": normalized,
            "approved_count": len(approve),
            "rejected_count": added_rejects,
            "pending_count": len(pending),
        }


class _HarvestTriggerBody(BaseModel):
    since_days: int | None = 90
    max_sessions: int = 100
    min_occurrences: int = 3
    min_sessions: int = 2
    anchors: list[str] | None = None


class _HarvestApproveItem(BaseModel):
    canonical: str
    observed: str | None = None
    layer: str = "alias"
    description: str | None = None


class _HarvestRejectItem(BaseModel):
    canonical: str
    observed: str


class _HarvestDecideBody(BaseModel):
    approve: list[_HarvestApproveItem] = []
    reject: list[_HarvestRejectItem] = []


@router.post(
    "/api/vocabulary/harvest",
    status_code=202,
    dependencies=[Depends(ratelimit.limit_api)],
)
def post_vocabulary_harvest(
    body: _HarvestTriggerBody = Body(default_factory=_HarvestTriggerBody),
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Queue a background vocabulary harvest over the active team's sessions."""
    from . import worker

    github, team_id, _is_admin = auth_triple
    if not config.meet_supports_vocab_harvest():
        raise HTTPException(
            501,
            "the installed millet CLI does not support `millet vocab harvest`; "
            "upgrade millet-pipeline and restart vezir",
        )
    if body.max_sessions < 1 or body.max_sessions > MAX_HARVEST_SESSIONS:
        raise HTTPException(400, f"max_sessions must be between 1 and {MAX_HARVEST_SESSIONS}")
    if body.min_occurrences < 1 or body.min_sessions < 1:
        raise HTTPException(400, "min_occurrences and min_sessions must be >= 1")
    if body.since_days is not None and body.since_days < 1:
        raise HTTPException(400, "since_days must be >= 1")

    cleaned_anchors: list[str] | None = None
    if body.anchors is not None:
        if len(body.anchors) > MAX_HARVEST_ANCHORS:
            raise HTTPException(400, f"anchors exceeds maximum of {MAX_HARVEST_ANCHORS}")
        try:
            cleaned_anchors = [
                _validate_piece(a, field="anchor", max_len=MAX_TERM_LEN) for a in body.anchors
            ]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    queued = worker.enqueue_task(
        "vocab_harvest",
        team_id,
        since_days=body.since_days,
        max_sessions=body.max_sessions,
        min_occurrences=body.min_occurrences,
        min_sessions=body.min_sessions,
        anchors=cleaned_anchors,
    )
    if queued:
        prev = load_team_harvest_state(team_id)
        save_team_harvest_state(
            team_id,
            {
                "status": "queued",
                "error": None,
                "updated_at": _now_utc_iso(),
                "report": prev.get("report"),
            },
        )
    log.info("team=%s vocab harvest requested by %s (queued=%s)", team_id, github, queued)
    return {"team_id": team_id, "queued": queued, "status": "queued" if queued else "running"}


@router.get("/api/vocabulary/harvest", dependencies=[Depends(ratelimit.limit_api)])
def get_vocabulary_harvest(
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Return latest harvest status, report metadata, and pending candidates."""
    _github, team_id, _is_admin = auth_triple
    state = load_team_harvest_state(team_id)
    vocab = load_team_vocabulary(team_id)
    rejected = load_team_rejected(team_id)
    pending = filter_pending_candidates(state.get("report"), vocab, rejected)
    return {
        "team_id": team_id,
        "status": state["status"],
        "error": state["error"],
        "updated_at": state["updated_at"],
        "report": state["report"],
        "pending_candidates": pending,
    }


@router.post("/api/vocabulary/harvest/decide", dependencies=[Depends(ratelimit.limit_api)])
def post_vocabulary_harvest_decide(
    body: _HarvestDecideBody = Body(...),
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Atomically approve harvested candidates into vocabulary.json or reject them."""
    github, team_id, _is_admin = auth_triple
    try:
        result = apply_harvest_decisions(
            team_id,
            approve=[item.model_dump() for item in body.approve],
            reject=[item.model_dump() for item in body.reject],
            decided_by=github,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info(
        "team=%s vocab harvest decided by %s (approved=%d, rejected=%d)",
        team_id,
        github,
        result["approved_count"],
        result["rejected_count"],
    )
    return result


@router.get("/api/vocabulary/rejected", dependencies=[Depends(ratelimit.limit_api)])
def get_vocabulary_rejected(
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """List dismissed harvest candidates for the active team."""
    _github, team_id, _is_admin = auth_triple
    return {"team_id": team_id, "rejected": load_team_rejected(team_id)}


@router.delete("/api/vocabulary/rejected", dependencies=[Depends(ratelimit.limit_api)])
def delete_vocabulary_rejected(
    canonical: str | None = Query(default=None),
    observed: str | None = Query(default=None),
    clear_all: bool = Query(default=False),
    auth_triple: tuple[str, str, bool] = Depends(auth.require_team_context),
) -> dict[str, Any]:
    """Remove a dismissed pair (or clear all) so it can be suggested again."""
    github, team_id, _is_admin = auth_triple
    try:
        kept, removed = remove_team_rejection(
            team_id, canonical=canonical, observed=observed, clear_all=clear_all
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("team=%s vocab rejected cleared by %s (removed=%d)", team_id, github, removed)
    return {"team_id": team_id, "removed": removed, "rejected": kept}
