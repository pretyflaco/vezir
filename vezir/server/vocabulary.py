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
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from .. import config
from . import auth, ratelimit

log = logging.getLogger("vezir.vocabulary")
router = APIRouter()

MAX_TERMS = 500
MAX_ALIASES_PER_TERM = 50
MAX_TERM_LEN = 100
MAX_DESC_LEN = 300

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
