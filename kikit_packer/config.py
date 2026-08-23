from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from .diagnostics import PackerError, error, warning
from .model import (
    AuthorityConfig,
    BoardConfig,
    CutsConfig,
    LayoutConfig,
    PageConfig,
    PanelConfig,
    PostConfig,
    ProjectConfig,
    TabsConfig,
)


class StrictLoader(yaml.SafeLoader):
    pass


StrictLoader.yaml_implicit_resolvers = {
    key: list(value) for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for first, resolvers in list(StrictLoader.yaml_implicit_resolvers.items()):
    StrictLoader.yaml_implicit_resolvers[first] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


def _construct_mapping(loader: StrictLoader, node, deep=False):
    output = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in output:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        output[key] = loader.construct_object(value_node, deep=deep)
    return output


StrictLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _fail(code: str, path: str, message: str, value: Any = None, exit_code: int = 2) -> NoReturn:
    raise PackerError(error(code, path, message, value), exit_code)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", path, "expected a mapping", value)
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail("UNKNOWN_KEY", path + "/" + unknown[0], "unknown configuration key")


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("INVALID_TYPE", path, "expected true or false", value)
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("INVALID_TYPE", path, "expected a non-empty string", value)
    return value


def _number(
    value: Any,
    path: str,
    minimum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("INVALID_TYPE", path, "expected a number", value)
    result = float(value)
    if not math.isfinite(result):
        _fail("NONFINITE_NUMBER", path, "number must be finite", value)
    if strictly_positive and result <= 0:
        _fail("OUT_OF_RANGE", path, "number must be greater than zero", value)
    if minimum is not None and result < minimum:
        _fail("OUT_OF_RANGE", path, f"number must be at least {minimum}", value)
    return result


def _integer(value: Any, path: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("INVALID_TYPE", path, "expected an integer", value)
    if positive and value <= 0:
        _fail("OUT_OF_RANGE", path, "integer must be greater than zero", value)
    return value


def _path(value: Any, base: Path, path: str, suffix: str | None = None) -> Path:
    raw = Path(_string(value, path)).expanduser()
    resolved = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
    if suffix is not None and resolved.suffix.lower() != suffix:
        _fail("INVALID_SUFFIX", path, f"expected a {suffix} path", value)
    return resolved


def _optional_positive(value: Any, path: str) -> float | None:
    return None if value is None else _number(value, path, strictly_positive=True)


def _parse_layout(raw: Any, path: str) -> LayoutConfig:
    data = _mapping({} if raw is None else raw, path)
    _keys(data, {"horizontal_spacing_mm", "vertical_spacing_mm", "rotation_deg", "rename_net", "rename_ref", "bake_text", "bake_ref"}, path)
    rotation = _number(data.get("rotation_deg", 0), path + "/rotation_deg")
    if rotation != 0:
        _fail("UNSUPPORTED_ROTATION", path + "/rotation_deg", "version 1 requires rotation_deg to be 0", rotation)
    return LayoutConfig(
        horizontal_spacing_mm=_number(data.get("horizontal_spacing_mm", 0), path + "/horizontal_spacing_mm", minimum=0),
        vertical_spacing_mm=_number(data.get("vertical_spacing_mm", 0), path + "/vertical_spacing_mm", minimum=0),
        rotation_deg=rotation,
        rename_net=_string(data.get("rename_net", "Board_{n}-{orig}"), path + "/rename_net"),
        rename_ref=_string(data.get("rename_ref", "{orig}"), path + "/rename_ref"),
        bake_text=_boolean(data.get("bake_text", True), path + "/bake_text"),
        bake_ref=_boolean(data.get("bake_ref", False), path + "/bake_ref"),
    )


def _parse_tabs(raw: Any, path: str) -> TabsConfig:
    data = _mapping({} if raw is None else raw, path)
    _keys(data, {"mode", "width_mm", "horizontal_count", "vertical_count", "min_distance_mm"}, path)
    mode = _string(data.get("mode", "flat-edge"), path + "/mode")
    if mode not in {"flat-edge", "fixed"}:
        _fail("INVALID_CHOICE", path + "/mode", "expected flat-edge or fixed", mode)
    result = TabsConfig(
        mode=mode,
        width_mm=_number(data.get("width_mm", 2), path + "/width_mm", strictly_positive=True),
        horizontal_count=_integer(data.get("horizontal_count", 1), path + "/horizontal_count", positive=True),
        vertical_count=_integer(data.get("vertical_count", 1), path + "/vertical_count", positive=True),
        min_distance_mm=_number(data.get("min_distance_mm", 0), path + "/min_distance_mm", minimum=0),
    )
    if mode == "flat-edge" and (result.horizontal_count != 1 or result.vertical_count != 1 or result.min_distance_mm != 0):
        _fail("IRRELEVANT_SETTING", path, "flat-edge tabs accept only width_mm")
    return result


def _parse_cuts(raw: Any, path: str) -> CutsConfig:
    data = _mapping({} if raw is None else raw, path)
    _keys(data, {"mode", "drill_mm", "spacing_mm", "offset_mm", "prolong_mm"}, path)
    mode = _string(data.get("mode", "none"), path + "/mode")
    if mode not in {"none", "mousebites"}:
        _fail("INVALID_CHOICE", path + "/mode", "expected none or mousebites", mode)
    return CutsConfig(
        mode=mode,
        drill_mm=_number(data.get("drill_mm", 0.5), path + "/drill_mm", strictly_positive=True),
        spacing_mm=_number(data.get("spacing_mm", 0.8), path + "/spacing_mm", strictly_positive=True),
        offset_mm=_number(data.get("offset_mm", 0), path + "/offset_mm", minimum=0),
        prolong_mm=_number(data.get("prolong_mm", 0), path + "/prolong_mm", minimum=0),
    )


def _parse_post(raw: Any, path: str) -> PostConfig:
    data = _mapping({} if raw is None else raw, path)
    _keys(data, {"mill_radius_mm", "origin", "refill_zones", "verify_refill_areas"}, path)
    origin = _string(data.get("origin", "top-left"), path + "/origin")
    if origin != "top-left":
        _fail("UNSUPPORTED_SETTING", path + "/origin", "version 1 supports top-left origin only", origin)
    refill = _boolean(data.get("refill_zones", False), path + "/refill_zones")
    if refill:
        _fail("ZONE_REFILL_FORBIDDEN", path + "/refill_zones", "zone refill is not supported")
    return PostConfig(
        mill_radius_mm=_number(data.get("mill_radius_mm", 1), path + "/mill_radius_mm", minimum=0),
        origin=origin,
        refill_zones=False,
        verify_refill_areas=_boolean(
            data.get("verify_refill_areas", False), path + "/verify_refill_areas"
        ),
    )


def _parse_page(raw: Any, path: str) -> PageConfig:
    data = _mapping({} if raw is None else raw, path)
    _keys(data, {"mode"}, path)
    mode = _string(data.get("mode", "inherit"), path + "/mode")
    if mode != "inherit":
        _fail("UNSUPPORTED_SETTING", path + "/mode", "version 1 supports inherited page only", mode)
    return PageConfig(mode)


def _parse_boards(raw: Any, base: Path, path: str) -> tuple[BoardConfig, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        _fail("INVALID_BOARDS", path, "boards must be a non-empty list", raw)
    boards = []
    for index, item in enumerate(raw):
        item_path = path + f"/{index}"
        data = _mapping(item, item_path)
        _keys(data, {"board", "qty", "margin_mm", "rotate"}, item_path)
        boards.append(BoardConfig(
            board=_path(data.get("board"), base, item_path + "/board", ".kicad_pcb"),
            qty=_integer(data.get("qty", 1), item_path + "/qty", positive=True),
            margin_mm=_number(data.get("margin_mm", 1), item_path + "/margin_mm", minimum=0),
            legacy_rotate=None if "rotate" not in data else _number(data["rotate"], item_path + "/rotate"),
        ))
    return tuple(boards)


def _check_collisions(panel: PanelConfig, boards: tuple[BoardConfig, ...]) -> None:
    if (
        panel.authority is not None
        and not panel.authority.reference_only
        and panel.authority.board not in {board.board for board in boards}
    ):
        _fail("AUTHORITY_NOT_PLACED", "/panel/authority/board", "placed authority must appear in boards")
    if panel.output is None:
        return
    manifest = Path(str(panel.output) + ".panel.json").resolve()
    inputs = {board.board.resolve() for board in boards}
    if panel.authority is not None:
        inputs.add(panel.authority.board.resolve())
        inputs.add(panel.authority.board.with_suffix(".kicad_pro").resolve())
        inputs.add(panel.authority.board.with_suffix(".kicad_dru").resolve())
    if panel.output.resolve() in inputs or manifest in inputs or panel.output.resolve() == manifest:
        _fail("PATH_COLLISION", "/panel/output", "output or manifest aliases an input")


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = yaml.load(handle, Loader=StrictLoader)
    except (OSError, yaml.YAMLError) as exc:
        _fail("YAML_ERROR", "", f"cannot load project: {exc}")
    return _mapping(value, "")


def load_project(
    path: Path,
    main_override: Path | None = None,
    output_override: Path | None = None,
    cwd: Path | None = None,
) -> ProjectConfig:
    path = path.expanduser().resolve()
    base = path.parent
    cwd = (cwd or Path.cwd()).resolve()
    root = _load_yaml(path)
    if "version" not in root:
        return _load_legacy(path, root, main_override, output_override, cwd)
    _keys(root, {"version", "panel", "boards"}, "")
    version = _integer(root.get("version"), "/version")
    if version != 1:
        _fail("UNSUPPORTED_VERSION", "/version", "only version 1 is supported", version)
    panel_data = _mapping(root.get("panel"), "/panel")
    _keys(panel_data, {"authority", "output", "max_width_mm", "max_height_mm", "layout", "tabs", "cuts", "post", "page", "allow_mixed_layers", "allow_mixed_thickness"}, "/panel")
    authority_data = _mapping(panel_data.get("authority"), "/panel/authority")
    _keys(authority_data, {"board", "reference_only"}, "/panel/authority")
    authority_path = _path(authority_data.get("board"), base, "/panel/authority/board", ".kicad_pcb")
    if main_override is not None:
        authority_path = (cwd / main_override).resolve() if not main_override.is_absolute() else main_override.resolve()
    authority = AuthorityConfig(authority_path, _boolean(authority_data.get("reference_only", False), "/panel/authority/reference_only"))
    output_value = output_override if output_override is not None else panel_data.get("output")
    output = None
    if output_value is not None:
        output_base = cwd if output_override is not None else base
        output = _path(str(output_value), output_base, "/panel/output", ".kicad_pcb")
    panel = PanelConfig(
        authority=authority,
        output=output,
        max_width_mm=_optional_positive(panel_data.get("max_width_mm"), "/panel/max_width_mm"),
        max_height_mm=_optional_positive(panel_data.get("max_height_mm"), "/panel/max_height_mm"),
        layout=_parse_layout(panel_data.get("layout"), "/panel/layout"),
        tabs=_parse_tabs(panel_data.get("tabs"), "/panel/tabs"),
        cuts=_parse_cuts(panel_data.get("cuts"), "/panel/cuts"),
        post=_parse_post(panel_data.get("post"), "/panel/post"),
        page=_parse_page(panel_data.get("page"), "/panel/page"),
        allow_mixed_layers=_boolean(panel_data.get("allow_mixed_layers", False), "/panel/allow_mixed_layers"),
        allow_mixed_thickness=_boolean(panel_data.get("allow_mixed_thickness", False), "/panel/allow_mixed_thickness"),
    )
    boards = _parse_boards(root.get("boards"), base, "/boards")
    _check_collisions(panel, boards)
    return ProjectConfig(1, path, panel, boards)


def _load_legacy(
    path: Path,
    root: Mapping[str, Any],
    main_override: Path | None,
    output_override: Path | None,
    cwd: Path,
) -> ProjectConfig:
    _keys(root, {"boards", "max_width", "max_height", "ignore_layer_count", "ignore_thickness"}, "")
    boards = _parse_boards(root.get("boards"), path.parent, "/boards")
    diagnostics = []
    for index, board in enumerate(boards):
        if board.legacy_rotate is not None:
            diagnostics.append(warning("LEGACY_ROTATE_IGNORED", f"/boards/{index}/rotate", "legacy rotate is accepted but has no effect"))
    authority = None
    if main_override is not None:
        main = (cwd / main_override).resolve() if not main_override.is_absolute() else main_override.resolve()
        authority = AuthorityConfig(main, main not in {board.board for board in boards})
    output = None
    if output_override is not None:
        output = (cwd / output_override).resolve() if not output_override.is_absolute() else output_override.resolve()
    panel = PanelConfig(
        authority=authority,
        output=output,
        max_width_mm=_optional_positive(root.get("max_width"), "/max_width"),
        max_height_mm=_optional_positive(root.get("max_height"), "/max_height"),
        allow_mixed_layers=_boolean(root.get("ignore_layer_count", False), "/ignore_layer_count"),
        allow_mixed_thickness=_boolean(root.get("ignore_thickness", False), "/ignore_thickness"),
    )
    _check_collisions(panel, boards)
    return ProjectConfig(0, path, panel, boards, legacy=True, diagnostics=tuple(diagnostics))
