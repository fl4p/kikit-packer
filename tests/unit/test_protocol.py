from pathlib import Path

import pytest

from kikit_packer.protocol import (
    ProtocolError,
    canonical_json_bytes,
    digest,
    load_json,
    resolve_staging_path,
    validate_envelope,
)


def test_canonical_digest_is_order_independent():
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_envelope_and_containment(tmp_path: Path):
    value = {
        "kind": "kikit-packer.run-contract",
        "schema_version": 1,
        "run_id": "run",
        "nonce": "a" * 64,
        "staging_root": str(tmp_path),
        "run_plan_path": "run-plan.json",
        "run_plan_sha256": "b" * 64,
        "staged_output": "artifacts/panel.kicad_pcb",
        "plugin_result_path": "plugin-result.json",
        "events_path": "events.jsonl",
        "log_limits": {
            "stdout_bytes": 1024,
            "stderr_bytes": 1024,
            "events_bytes": 1024,
        },
    }
    validate_envelope(value, "kikit-packer.run-contract")
    assert resolve_staging_path(tmp_path, "a/b") == (tmp_path / "a/b").resolve()
    with pytest.raises(ProtocolError):
        resolve_staging_path(tmp_path, "../escape")
    with pytest.raises(ProtocolError):
        resolve_staging_path(tmp_path, str(tmp_path / "absolute"))


def test_duplicate_json_key_is_rejected(tmp_path: Path):
    path = tmp_path / "value.json"
    path.write_text('{"kind":"a","kind":"b"}')
    with pytest.raises(ProtocolError):
        load_json(path)


def test_strict_types_unknown_fields_and_size_limit(tmp_path: Path):
    event = {
        "kind": "kikit-packer.event",
        "schema_version": 1,
        "run_id": "run",
        "nonce": "a" * 64,
        "sequence": 1,
        "stage": "plan",
        "event": "completed",
        "payload": {},
    }
    validate_envelope(event, "kikit-packer.event")
    invalid = dict(event, sequence=True)
    with pytest.raises(ProtocolError, match="type"):
        validate_envelope(invalid, "kikit-packer.event")
    invalid = dict(event, unexpected="field")
    with pytest.raises(ProtocolError, match="unknown"):
        validate_envelope(invalid, "kikit-packer.event")
    invalid = dict(event, payload={"float": 1.5})
    with pytest.raises(ProtocolError, match="unsupported"):
        validate_envelope(invalid, "kikit-packer.event")
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    with pytest.raises(ProtocolError, match="size"):
        load_json(path)
