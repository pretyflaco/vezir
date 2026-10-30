"""Streaming multipart/form-data for the upload endpoints.

Why not FastAPI's ``File(...)`` / ``UploadFile``?  FastAPI resolves those by
calling ``request.form()`` *before* the handler body runs, and Starlette's
parser spools every file part to a ``SpooledTemporaryFile`` with no size cap.
Two consequences (security audit 2026-10-30, H-4 / H-4b):

* the handler's own size checks run only after the entire body is already
  on the server's temp disk, so one authenticated request could fill it;
* on a mid-upload ``ClientDisconnect`` Starlette closes those spool files
  only for ``MultiPartException``, and FastAPI registers ``body.close`` only
  after ``form()`` returns — so the partially written spool (up to the full
  body size) stays pinned in a ``raise … from e`` reference cycle until a
  full GC, i.e. effectively until the process restarts.  Observed on saray:
  a 2.1 GB deleted ``O_TMPFILE`` held open at 0 % CPU.

This module drives ``python-multipart``'s push parser over
``request.stream()`` and hands file bytes straight to a caller-provided
sink.  Nothing is spooled; the sink enforces its own bounds as bytes
arrive; every sink is closed in a ``finally`` on any failure, so the
handler's cleanup sees a consistent state.

Wire format is unchanged — clients, TestClient ``files=``/``data=`` and
curl ``-F`` all keep working.  Only the handler signature loses the
auto-generated OpenAPI form schema, which nothing consumes (JSON-only API).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import HTTPException, Request

# Non-file fields are small: titles, presets, booleans, a byte count.
MAX_FIELDS = 64
MAX_FIELD_BYTES = 64 * 1024
# A single part's header block (Content-Disposition + Content-Type).
MAX_PART_HEADER_BYTES = 16 * 1024

_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;,\s]+))', re.IGNORECASE)
_DISPOSITION_PARAM_RE = re.compile(r';\s*([a-zA-Z0-9_*-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))')


class FileSink(Protocol):
    """Where a file part's bytes go.  Raise ``HTTPException`` to reject."""

    def write(self, chunk: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass
class PartInfo:
    """Headers of one multipart part, as seen before any body bytes."""

    name: str
    filename: str | None
    content_type: str | None


@dataclass
class _State:
    headers: dict[str, str] = field(default_factory=dict)
    header_name: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    header_bytes: int = 0
    info: PartInfo | None = None
    sink: FileSink | None = None
    field_buf: bytearray = field(default_factory=bytearray)
    fields: dict[str, str] = field(default_factory=dict)
    sinks_open: list[FileSink] = field(default_factory=list)
    files_seen: int = 0
    ended: bool = False


def _parse_boundary(content_type: str | None) -> bytes:
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="expected multipart/form-data")
    m = _BOUNDARY_RE.search(content_type)
    if not m:
        raise HTTPException(status_code=400, detail="multipart boundary missing")
    boundary = m.group(1) or m.group(2) or ""
    if not boundary or len(boundary) > 200:
        raise HTTPException(status_code=400, detail="multipart boundary invalid")
    return boundary.encode("latin-1")


def _parse_disposition(value: str) -> tuple[str | None, str | None]:
    """Return (name, filename) from a Content-Disposition value."""
    name: str | None = None
    filename: str | None = None
    for m in _DISPOSITION_PARAM_RE.finditer(value):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        val = val.replace('\\"', '"').strip()
        if key == "name":
            name = val
        elif key == "filename":
            filename = val
        elif key == "filename*" and filename is None:
            # RFC 5987: charset'lang'percent-encoded
            try:
                from urllib.parse import unquote

                filename = unquote(val.split("'", 2)[-1])
            except Exception:
                pass
    return name, filename


def _decode(buf: bytes) -> str:
    try:
        return buf.decode("utf-8")
    except UnicodeDecodeError:
        return buf.decode("latin-1")


async def stream_multipart(
    request: Request,
    open_file: Callable[[PartInfo], FileSink],
    *,
    max_body_bytes: int | None = None,
    max_files: int | None = None,
) -> dict[str, str]:
    """Parse the request body, routing file parts to ``open_file`` sinks.

    Returns the non-file fields (last value wins for a repeated name).
    ``open_file`` is called once per file part with its headers and must
    return a sink; it may raise ``HTTPException`` to reject the part
    (nothing of that part has been read yet).  Every opened sink is closed
    before this function returns or raises.

    ``max_body_bytes`` bounds the raw body read (413 if exceeded, and the
    declared ``Content-Length`` is checked before the first read).
    """
    boundary = _parse_boundary(request.headers.get("content-type"))

    if max_body_bytes is not None:
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > max_body_bytes:
                    raise HTTPException(status_code=413, detail="upload too large")
            except ValueError:
                pass

    import python_multipart as multipart

    st = _State()

    # ── parser callbacks (synchronous, run inside parser.write) ──────────
    def on_part_begin() -> None:
        st.headers = {}
        st.header_bytes = 0
        st.info = None
        st.sink = None
        st.field_buf = bytearray()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        st.header_bytes += end - start
        if st.header_bytes > MAX_PART_HEADER_BYTES:
            raise HTTPException(status_code=400, detail="multipart part headers too large")
        st.header_name += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        st.header_bytes += end - start
        if st.header_bytes > MAX_PART_HEADER_BYTES:
            raise HTTPException(status_code=400, detail="multipart part headers too large")
        st.header_value += data[start:end]

    def on_header_end() -> None:
        st.headers[_decode(bytes(st.header_name)).lower()] = _decode(bytes(st.header_value))
        st.header_name = bytearray()
        st.header_value = bytearray()

    def on_headers_finished() -> None:
        name, filename = _parse_disposition(st.headers.get("content-disposition", ""))
        if not name:
            raise HTTPException(status_code=400, detail="multipart part without a name")
        if filename is not None:
            if max_files is not None and st.files_seen >= max_files:
                raise HTTPException(status_code=413, detail="too many files")
            st.files_seen += 1
            st.info = PartInfo(
                name=name, filename=filename, content_type=st.headers.get("content-type"),
            )
            st.sink = open_file(st.info)
            st.sinks_open.append(st.sink)
        else:
            st.info = PartInfo(
                name=name, filename=None, content_type=st.headers.get("content-type"),
            )
            if len(st.fields) >= MAX_FIELDS and name not in st.fields:
                raise HTTPException(status_code=400, detail="too many form fields")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if st.sink is not None:
            st.sink.write(data[start:end])
            return
        st.field_buf += data[start:end]
        if len(st.field_buf) > MAX_FIELD_BYTES:
            raise HTTPException(status_code=400, detail="form field too large")

    def on_part_end() -> None:
        if st.sink is not None:
            st.sink.close()
            st.sinks_open.remove(st.sink)
            st.sink = None
        elif st.info is not None:
            st.fields[st.info.name] = _decode(bytes(st.field_buf))
        st.info = None
        st.field_buf = bytearray()

    def on_end() -> None:
        st.ended = True

    parser = multipart.MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_end": on_end,
        },
    )

    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if max_body_bytes is not None and received > max_body_bytes:
                raise HTTPException(status_code=413, detail="upload too large")
            try:
                parser.write(chunk)
            except HTTPException:
                raise
            except multipart.exceptions.FormParserError as exc:
                raise HTTPException(
                    status_code=400, detail=f"malformed multipart body: {exc}",
                ) from exc
        try:
            parser.finalize()
        except multipart.exceptions.FormParserError as exc:
            raise HTTPException(status_code=400, detail=f"malformed multipart body: {exc}") from exc
        if not st.ended:
            raise HTTPException(
                status_code=400, detail="incomplete multipart body: closing boundary missing",
            )
    finally:
        # A disconnect, a parser error, or a sink rejection: every sink the
        # handler opened is closed here so its cleanup can delete the files.
        for s in list(st.sinks_open):
            try:
                s.close()
            except Exception:
                pass
        st.sinks_open.clear()

    return st.fields
