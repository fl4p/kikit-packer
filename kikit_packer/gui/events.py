from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol import ProtocolError, validate_envelope


@dataclass(frozen=True)
class EventCursor:
    offset: int = 0
    sequence: int = 0


def _object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ProtocolError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def read_events(
    path: Path,
    cursor: EventCursor,
    *,
    run_id: str,
    nonce: str,
    max_bytes: int,
) -> tuple[EventCursor, list[dict[str, Any]]]:
    if not path.exists():
        return cursor, []
    size = path.stat().st_size
    if size > max_bytes:
        raise ProtocolError("event stream exceeds configured limit")
    with path.open("rb") as handle:
        handle.seek(cursor.offset)
        data = handle.read()
    complete = data.rfind(b"\n")
    if complete < 0:
        return cursor, []
    consumed = data[: complete + 1]
    sequence = cursor.sequence
    events = []
    for encoded in consumed.splitlines():
        try:
            value = json.loads(
                encoded,
                object_pairs_hook=_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ProtocolError(f"invalid number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid event JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolError("event must be an object")
        validate_envelope(value, "kikit-packer.event")
        if value["run_id"] != run_id or value["nonce"] != nonce:
            raise ProtocolError("event belongs to another run")
        if value["sequence"] != sequence + 1:
            raise ProtocolError("event sequence is not contiguous")
        sequence = value["sequence"]
        events.append(value)
    return EventCursor(cursor.offset + len(consumed), sequence), events
