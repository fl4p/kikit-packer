from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .protocol import digest

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:\\.|[^"\\])*)"|([^\s()]+))')
KNOWN_GLOBAL = {
    "copper_finish",
    "dielectric_constraints",
    "edge_connector",
    "castellated_pads",
    "edge_plating",
}
KNOWN_LAYER = {
    "type",
    "color",
    "thickness",
    "material",
    "epsilon_r",
    "loss_tangent",
}
DECIMAL_FIELDS = {"epsilon_r", "loss_tangent"}


class StackupParseError(ValueError):
    pass


def _tokens(text: str) -> Iterator[str]:
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            if text[position:].strip():
                raise StackupParseError(f"invalid S-expression at byte {position}")
            return
        position = match.end()
        if match.group(1):
            yield "("
        elif match.group(2):
            yield ")"
        elif match.group(3) is not None:
            yield bytes(match.group(3), "utf-8").decode("unicode_escape")
        elif match.group(4):
            yield match.group(4)


def _parse(tokens: Iterator[str]) -> list[Any]:
    root: list[Any] = []
    stack = [root]
    for token in tokens:
        if token == "(":
            child: list[Any] = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            if len(stack) == 1:
                raise StackupParseError("unexpected closing parenthesis")
            stack.pop()
        else:
            stack[-1].append(token)
    if len(stack) != 1:
        raise StackupParseError("unclosed parenthesis")
    return root


def _find_named(value: Any, name: str) -> list[Any] | None:
    if isinstance(value, list):
        if value and value[0] == name:
            return value
        for item in value:
            found = _find_named(item, name)
            if found is not None:
                return found
    return None


def _find_stackup(value: Any) -> list[Any] | None:
    return _find_named(value, "stackup")


def parse_setup_digest(path: Path) -> str:
    tree = _parse(_tokens(path.read_text(encoding="utf-8")))
    setup = _find_named(tree, "setup")
    if setup is None:
        raise StackupParseError("board has no setup section")
    panel_owned = {"aux_axis_origin", "grid_origin"}
    normalized = [setup[0]] + [
        item
        for item in setup[1:]
        if not (isinstance(item, list) and item and item[0] in panel_owned)
    ]
    return digest(normalized)


def _decimal(value: str) -> str:
    number = Decimal(value)
    if not number.is_finite():
        raise InvalidOperation
    normalized = number.normalize()
    return format(normalized, "f")


def _length_iu(value: str) -> int:
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise InvalidOperation
    return int((number * Decimal(1_000_000)).to_integral_exact())


def _normalize(stackup: list[Any]) -> dict[str, Any]:
    layers = []
    globals_: dict[str, Any] = {}
    problems = []
    unknown = []
    for item in stackup[1:]:
        if not isinstance(item, list) or len(item) < 2:
            problems.append("malformed stackup item")
            continue
        key = str(item[0])
        if key == "layer":
            name = str(item[1])
            fields: dict[str, Any] = {}
            for field in item[2:]:
                if not isinstance(field, list) or len(field) != 2:
                    problems.append(f"malformed layer field in {name}")
                    continue
                field_name = str(field[0])
                if field_name not in KNOWN_LAYER:
                    unknown.append(f"layer.{field_name}")
                    continue
                raw = str(field[1])
                try:
                    if field_name == "thickness":
                        fields[field_name + "_iu"] = _length_iu(raw)
                    elif field_name in DECIMAL_FIELDS:
                        fields[field_name] = _decimal(raw)
                    else:
                        fields[field_name] = raw
                except (InvalidOperation, ValueError):
                    problems.append(f"invalid {field_name} in {name}")
            layer_type = fields.get("type")
            if layer_type is None:
                problems.append(f"missing type in {name}")
            if layer_type == "copper" and "thickness_iu" not in fields:
                problems.append(f"missing copper thickness in {name}")
            if layer_type in {"core", "prepreg"}:
                for required in ("thickness_iu", "material", "epsilon_r", "loss_tangent"):
                    if required not in fields:
                        problems.append(f"missing {required} in {name}")
            layers.append({"name": name, "fields": fields})
        else:
            if key not in KNOWN_GLOBAL:
                unknown.append(key)
                continue
            if len(item) != 2:
                problems.append(f"malformed global field {key}")
            else:
                globals_[key] = str(item[1])
    for required in ("copper_finish", "dielectric_constraints"):
        if required not in globals_:
            problems.append(f"missing global {required}")
    if not layers:
        problems.append("stackup has no layers")
    return {
        "layers": layers,
        "globals": globals_,
        "unknown_keys": sorted(set(unknown)),
        "problems": sorted(set(problems)),
    }


def parse_stackup(path: Path) -> dict[str, Any]:
    try:
        tree = _parse(_tokens(path.read_text(encoding="utf-8")))
        stackup = _find_stackup(tree)
        if stackup is None:
            return {
                "present": False,
                "verified": False,
                "descriptor": None,
                "unknown_keys": [],
                "problems": [],
            }
        normalized = _normalize(stackup)
        verified = not normalized["unknown_keys"] and not normalized["problems"]
        return {
            "present": True,
            "verified": verified,
            "descriptor": {
                "layers": normalized["layers"],
                "globals": normalized["globals"],
            },
            "unknown_keys": normalized["unknown_keys"],
            "problems": normalized["problems"],
        }
    except (OSError, StackupParseError, UnicodeError) as exc:
        return {
            "present": True,
            "verified": False,
            "descriptor": None,
            "unknown_keys": [],
            "problems": [str(exc)],
        }
