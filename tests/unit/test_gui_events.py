import json
from pathlib import Path

import pytest

from kikit_packer.gui.events import EventCursor, read_events
from kikit_packer.protocol import ProtocolError


def event(sequence: int):
    return {
        "kind": "kikit-packer.event",
        "schema_version": 1,
        "run_id": "run-1",
        "nonce": "a" * 64,
        "sequence": sequence,
        "stage": "generate",
        "event": "started",
        "payload": {},
    }


def test_reads_only_complete_contiguous_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    first = json.dumps(event(1)).encode()
    second = json.dumps(event(2)).encode()
    path.write_bytes(first + b"\n" + second)

    cursor, values = read_events(
        path,
        EventCursor(),
        run_id="run-1",
        nonce="a" * 64,
        max_bytes=4096,
    )
    assert [value["sequence"] for value in values] == [1]
    assert cursor.sequence == 1

    with path.open("ab") as handle:
        handle.write(b"\n")
    cursor, values = read_events(
        path,
        cursor,
        run_id="run-1",
        nonce="a" * 64,
        max_bytes=4096,
    )
    assert [value["sequence"] for value in values] == [2]
    assert cursor.sequence == 2


def test_rejects_wrong_run_sequence_and_oversized_stream(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event(2)) + "\n")
    with pytest.raises(ProtocolError, match="sequence"):
        read_events(path, EventCursor(), run_id="run-1", nonce="a" * 64, max_bytes=4096)
    with pytest.raises(ProtocolError, match="exceeds"):
        read_events(path, EventCursor(), run_id="run-1", nonce="a" * 64, max_bytes=10)
