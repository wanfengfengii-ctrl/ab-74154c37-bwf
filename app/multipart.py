"""Minimal, byte-exact multipart/form-data parser (RFC 7578 subset).

Implemented on the standard library only so the service has no third-party
dependencies.  The parser splits on the exact boundary byte sequence, so
binary payloads are never re-encoded or normalized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_BOUNDARY_RE = re.compile(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)


class MultipartError(Exception):
    """A locatable, client-caused (4xx) multipart problem."""

    def __init__(self, code: str, message: str, *, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.field is not None:
            out["field"] = self.field
        return out


@dataclass
class Part:
    name: str
    filename: Optional[str]
    content_type: Optional[str]
    data: bytes


def _split_header_params(value: str) -> List[str]:
    """Split a header value on ';' while respecting quoted strings."""
    segments, current = [], []
    in_quotes = False
    escaped = False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and in_quotes:
            escaped = True
        elif ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == ";" and not in_quotes:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return segments


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        out, escaped = [], False
        for ch in inner:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            else:
                out.append(ch)
        if escaped:
            out.append("\\")
        return "".join(out)
    return value


def parse_header_params(value: str) -> Tuple[str, Dict[str, str]]:
    segments = _split_header_params(value)
    main = segments[0].strip().lower()
    params: Dict[str, str] = {}
    for segment in segments[1:]:
        if "=" not in segment:
            continue
        key, _, raw = segment.partition("=")
        params[key.strip().lower()] = _unquote(raw)
    return main, params


def extract_boundary(content_type: str) -> bytes:
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        raise MultipartError(
            "unsupported_media_type",
            "Content-Type must be multipart/form-data",
        )
    match = _BOUNDARY_RE.search(content_type)
    if not match:
        raise MultipartError(
            "missing_boundary",
            "multipart Content-Type carries no boundary parameter",
        )
    boundary = match.group(1) if match.group(1) is not None else match.group(2)
    try:
        boundary_bytes = boundary.encode("ascii")
    except UnicodeEncodeError:
        raise MultipartError("invalid_boundary", "boundary is not ASCII") from None
    if not 1 <= len(boundary_bytes) <= 70:
        raise MultipartError(
            "invalid_boundary",
            f"boundary length {len(boundary_bytes)} is outside 1-70 characters",
        )
    return boundary_bytes


def parse_multipart(body: bytes, content_type: str) -> List[Part]:
    boundary = extract_boundary(content_type)
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        raise MultipartError(
            "malformed_multipart",
            "body does not start with the boundary delimiter",
        )

    parts: List[Part] = []
    closed = False
    for segment in body.split(delimiter)[1:]:
        if segment.startswith(b"--"):
            closed = True
            break
        if not segment.startswith(b"\r\n"):
            raise MultipartError(
                "malformed_multipart",
                "boundary delimiter is not followed by CRLF",
            )
        segment = segment[2:]
        header_end = segment.find(b"\r\n\r\n")
        if header_end < 0:
            raise MultipartError(
                "malformed_multipart",
                "part headers are not terminated by an empty line",
            )
        raw_headers = segment[:header_end].decode("utf-8", "replace")
        payload = segment[header_end + 4:]
        if not payload.endswith(b"\r\n"):
            raise MultipartError(
                "malformed_multipart",
                "part payload is not terminated by CRLF before the next boundary",
            )
        payload = payload[:-2]

        headers: Dict[str, str] = {}
        for line in raw_headers.split("\r\n"):
            if not line:
                continue
            if ":" not in line:
                raise MultipartError(
                    "malformed_header",
                    f"part header line has no colon: {line!r}",
                )
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()

        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MultipartError(
                "missing_disposition",
                "part is missing its Content-Disposition header",
            )
        disp_main, disp_params = parse_header_params(disposition)
        if disp_main != "form-data":
            raise MultipartError(
                "invalid_disposition",
                f"Content-Disposition must be form-data, got {disp_main!r}",
            )
        name = disp_params.get("name")
        if not name:
            raise MultipartError(
                "missing_field_name",
                "form-data part has no name parameter",
            )
        parts.append(
            Part(
                name=name,
                filename=disp_params.get("filename"),
                content_type=headers.get("content-type"),
                data=payload,
            )
        )

    if not closed:
        raise MultipartError(
            "malformed_multipart",
            "closing boundary delimiter is missing",
        )
    return parts


def single_part(parts: List[Part], name: str) -> Part:
    """Return the unique part called *name* or raise a locatable error."""
    matches = [p for p in parts if p.name == name]
    if not matches:
        raise MultipartError(
            "missing_field",
            f"required form field '{name}' is missing",
            field=name,
        )
    if len(matches) > 1:
        raise MultipartError(
            "duplicate_field",
            f"form field '{name}' was supplied {len(matches)} times",
            field=name,
        )
    return matches[0]


_UINT_RE = re.compile(rb"\d{1,20}")


def parse_uint(part: Part) -> int:
    """Parse a form part as a non-negative decimal integer."""
    text = part.data.strip()
    if not _UINT_RE.fullmatch(text):
        raise MultipartError(
            "invalid_parameter",
            f"field '{part.name}' must be a non-negative decimal integer",
            field=part.name,
        )
    return int(text)
