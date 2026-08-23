from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .diagnostics import Diagnostic


@dataclass(frozen=True)
class AuthorityConfig:
    board: Path
    reference_only: bool = False


@dataclass(frozen=True)
class LayoutConfig:
    horizontal_spacing_mm: float = 0.0
    vertical_spacing_mm: float = 0.0
    rotation_deg: float = 0.0
    rename_net: str = "Board_{n}-{orig}"
    rename_ref: str = "{orig}"
    bake_text: bool = True
    bake_ref: bool = False


@dataclass(frozen=True)
class TabsConfig:
    mode: str = "flat-edge"
    width_mm: float = 2.0
    horizontal_count: int = 1
    vertical_count: int = 1
    min_distance_mm: float = 0.0


@dataclass(frozen=True)
class CutsConfig:
    mode: str = "none"
    drill_mm: float = 0.5
    spacing_mm: float = 0.8
    offset_mm: float = 0.0
    prolong_mm: float = 0.0


@dataclass(frozen=True)
class PostConfig:
    mill_radius_mm: float = 1.0
    origin: str = "top-left"
    refill_zones: bool = False
    verify_refill_areas: bool = False


@dataclass(frozen=True)
class PageConfig:
    mode: str = "inherit"


@dataclass(frozen=True)
class PanelConfig:
    authority: AuthorityConfig | None
    output: Path | None
    max_width_mm: float | None = None
    max_height_mm: float | None = None
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    tabs: TabsConfig = field(default_factory=TabsConfig)
    cuts: CutsConfig = field(default_factory=CutsConfig)
    post: PostConfig = field(default_factory=PostConfig)
    page: PageConfig = field(default_factory=PageConfig)
    allow_mixed_layers: bool = False
    allow_mixed_thickness: bool = False


@dataclass(frozen=True)
class BoardConfig:
    board: Path
    qty: int = 1
    margin_mm: float = 1.0
    legacy_rotate: float | None = None


@dataclass(frozen=True)
class ProjectConfig:
    version: int
    source_path: Path
    panel: PanelConfig
    boards: tuple[BoardConfig, ...]
    legacy: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class BoardInspection:
    source_id: str
    path: Path
    sha256: str
    outline_bounds_iu: tuple[int, int, int, int]
    copper_bounds_iu: tuple[int, int, int, int]
    copper_layers: tuple[str, ...]
    copper_layer_count: int
    thickness_iu: int
    setup_sha256: str
    stackup: dict[str, object] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Placement:
    instance_id: str
    source_id: str
    x_iu: int
    y_iu: int
    rotated: bool
    width_iu: int
    height_iu: int


@dataclass(frozen=True)
class PackingResult:
    placements: tuple[Placement, ...]
    bounds_iu: tuple[int, int, int, int]
    candidate_count: int
    evaluated_count: int
