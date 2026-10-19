"""Opaque keyset cursors for every paginated list.

A page is ordered by `(sort_key DESC, id DESC)` and the cursor carries the last row's pair, so the next
page is `WHERE (sort_key, id) < (cursor.key, cursor.id)`. Because `id` is unique the pair is a total
order: rows that share a timestamp can never be skipped or repeated across a page boundary (a cursor on
the timestamp alone loses every tied row after the boundary).

Clients treat the cursor as an opaque string (urlsafe base64 of a small JSON document). Lists that
return a bare JSON array put the next cursor in the `X-Next-Cursor` response header (absent on the last
page); lists with an envelope return it as `next_before`. Pass it back as `?before=`.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.core.errors import AppError

NEXT_CURSOR_HEADER = "X-Next-Cursor"
CURSOR_DOC = "Opaque cursor from the previous page (X-Next-Cursor header or next_before)"
_VERSION = 1


@dataclass(frozen=True)
class Cursor:
    key: datetime | int
    id: uuid.UUID | int | None


def encode_cursor(key: datetime | int, row_id: uuid.UUID | int | None) -> str:
    k: str | int = key.astimezone(UTC).isoformat() if isinstance(key, datetime) else key
    i: str | int | None = str(row_id) if isinstance(row_id, uuid.UUID) else row_id
    raw = json.dumps({"v": _VERSION, "k": k, "i": i}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _bad() -> AppError:
    return AppError(
        400, "bad_cursor", "the pagination cursor is not valid; start from the first page"
    )


def decode_cursor(
    raw: str,
    *,
    key: Literal["time", "int"],
    id_type: type[uuid.UUID] | type[int] | None,
) -> Cursor:
    """Parse and validate a cursor produced by `encode_cursor` for the same list."""
    try:
        doc = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, binascii.Error) as exc:
        raise _bad() from exc
    if not isinstance(doc, dict) or doc.get("v") != _VERSION:
        raise _bad()
    k, i = doc.get("k"), doc.get("i")
    try:
        if key == "time":
            if not isinstance(k, str):
                raise _bad()
            ts = datetime.fromisoformat(k)
            if ts.tzinfo is None:
                raise _bad()
            parsed_key: datetime | int = ts
        else:
            if not isinstance(k, int) or isinstance(k, bool):
                raise _bad()
            parsed_key = k
        if id_type is None:
            if i is not None:
                raise _bad()
            return Cursor(parsed_key, None)
        if id_type is uuid.UUID:
            if not isinstance(i, str):
                raise _bad()
            return Cursor(parsed_key, uuid.UUID(i))
        if not isinstance(i, int) or isinstance(i, bool):
            raise _bad()
        return Cursor(parsed_key, i)
    except ValueError as exc:
        raise _bad() from exc
