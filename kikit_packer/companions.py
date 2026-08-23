import copy
import json
from pathlib import Path
from typing import Any

from .protocol import digest


class CompanionError(ValueError):
    pass


def _load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CompanionError(f"project companion must be a JSON object: {path}")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def project_authority_profile(path: Path) -> dict[str, Any]:
    value = _jsonable(copy.deepcopy(_load_object(path)))
    meta = value.get("meta")
    if isinstance(meta, dict):
        meta.pop("filename", None)
    schematic = value.get("schematic")
    if isinstance(schematic, dict):
        schematic.pop("top_level_sheets", None)
    net_settings = value.get("net_settings")
    classes = []
    assignments = {}
    if isinstance(net_settings, dict):
        raw_classes = net_settings.pop("classes", [])
        raw_assignments = net_settings.pop("netclass_assignments", {})
        if isinstance(raw_assignments, dict):
            assignments = raw_assignments
        if isinstance(raw_classes, list):
            classes = raw_classes
    return {
        "base_sha256": digest(value),
        "net_classes": classes,
        "netclass_assignments": assignments,
    }


def verify_project_authority(path: Path, expected: dict[str, Any]) -> None:
    actual = project_authority_profile(path)
    if actual["base_sha256"] != expected.get("base_sha256"):
        raise CompanionError("project companion authority-owned settings changed")
    actual_classes = {
        item.get("name"): item
        for item in actual["net_classes"]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if actual["netclass_assignments"] != expected.get("netclass_assignments", {}):
        raise CompanionError("authority netclass assignments changed")
    for expected_class in expected.get("net_classes", []):
        if not isinstance(expected_class, dict):
            raise CompanionError("authority net class is malformed")
        name = expected_class.get("name")
        if actual_classes.get(name) != expected_class:
            raise CompanionError(f"authority net class changed or disappeared: {name}")
