import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = {
    "kikit-packer.run-plan",
    "kikit-packer.run-contract",
    "kikit-packer.plugin-result",
    "kikit-packer.event",
}


class ProtocolError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ProtocolError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=reject_duplicate, parse_constant=lambda x: (_ for _ in ()).throw(ProtocolError("invalid number: " + x)))
    if not isinstance(value, dict):
        raise ProtocolError("protocol document must be an object")
    return value


def _require(value: dict[str, Any], fields: dict[str, type]) -> None:
    for field, expected in fields.items():
        if field not in value:
            raise ProtocolError(f"missing protocol field: {field}")
        if not isinstance(value[field], expected):
            raise ProtocolError(f"invalid type for protocol field: {field}")


def validate_envelope(value: dict[str, Any], expected_kind: str) -> None:
    if expected_kind not in KINDS:
        raise ValueError("unknown expected protocol kind")
    if value.get("kind") != expected_kind:
        raise ProtocolError("expected {}, got {}".format(expected_kind, value.get("kind")))
    if value.get("schema_version") != 1:
        raise ProtocolError("unsupported schema version")
    nonce = value.get("nonce")
    if nonce is not None and not HASH_RE.fullmatch(str(nonce)):
        raise ProtocolError("nonce must be 64 lowercase hexadecimal characters")
    if expected_kind == "kikit-packer.run-plan":
        _require(value, {
            "run_id": str,
            "nonce": str,
            "project_digest": str,
            "runtime": dict,
            "authority": dict,
            "sources": list,
            "instances": list,
            "packing": dict,
            "resolved_settings": dict,
            "diagnostics": list,
        })
        if not HASH_RE.fullmatch(value["project_digest"]):
            raise ProtocolError("invalid project digest")
        if not value["sources"] or not value["instances"]:
            raise ProtocolError("run plan must contain sources and instances")
    elif expected_kind == "kikit-packer.run-contract":
        _require(value, {
            "run_id": str,
            "nonce": str,
            "staging_root": str,
            "run_plan_path": str,
            "run_plan_sha256": str,
            "staged_output": str,
            "plugin_result_path": str,
            "events_path": str,
            "log_limits": dict,
        })
        if not Path(value["staging_root"]).is_absolute():
            raise ProtocolError("staging root must be absolute")
        if not HASH_RE.fullmatch(value["run_plan_sha256"]):
            raise ProtocolError("invalid run plan hash")
        for field in ("run_plan_path", "staged_output", "plugin_result_path", "events_path"):
            if Path(value[field]).is_absolute():
                raise ProtocolError("contract child paths must be relative")
    elif expected_kind == "kikit-packer.plugin-result":
        _require(value, {
            "run_id": str,
            "nonce": str,
            "run_plan_sha256": str,
            "lifecycle": dict,
            "instances": list,
            "tabs": dict,
            "cuts": dict,
            "inventories": dict,
            "artifacts": list,
            "diagnostics": list,
        })
    else:
        _require(value, {
            "run_id": str,
            "nonce": str,
            "sequence": int,
            "stage": str,
            "event": str,
            "payload": dict,
        })


def append_event(path: Path, value: dict[str, Any]) -> None:
    validate_envelope(value, "kikit-packer.event")
    encoded = canonical_json_bytes(value) + b"\n"
    if len(encoded) > 65536:
        raise ProtocolError("event exceeds 64 KiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def resolve_staging_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ProtocolError("protocol child path must be relative")
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ProtocolError(f"protocol path escapes staging root: {relative}")
    return candidate
