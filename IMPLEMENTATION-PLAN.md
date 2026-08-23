# KiKit Packer UX and Installer Implementation Plan

## Status and baseline

This is an implementation-ready plan. It does not itself change panelization behavior.

The repository is at `21417c7`. `kikit-packer.py` has an uncommitted 188-line behavior change that
adds mixed-layer/thickness handling, copper-aware packing, `FlatEdgeTabs`, and validation. A local
KiCad 10.0.5 / KiKit 1.8.1 run of the tracked example succeeds. That work is the baseline and must
be qualified before it is moved. It must not be overwritten, committed implicitly, or mixed with a
behavior-preserving relocation.

## 1. Objective and first-release boundary

Provide three supported entry points over one implementation:

```text
kikit-packer gui [project.yaml]
kikit-packer pack project.yaml [--main authority.kicad_pcb]
    [--output panel.kicad_pcb] [--max-rotation-candidates N] [--open]
kikit-packer doctor [project.yaml] [--json]
```

The GUI is the primary first-use experience. The CLI is the reproducible automation interface. The
existing path-loaded KiKit layout-plugin invocation remains supported for legacy YAML.

The first release includes project parsing, source inspection, deterministic packing, KiKit
execution, semantic verification, manifests, a wx GUI, runtime discovery, bootstrap installers,
and uninstall. It does not promise a platform/version matrix cell until the Phase 0 feasibility
probe and release smoke test for that cell pass.

## 2. Safety and fabrication policy

### 2.1 Panel authority

A physical panel has one layer stack and one thickness. Version 1 uses an explicit authority:

```yaml
panel:
  authority:
    board: ../../hw/controller/controller.kicad_pcb
    reference_only: false
```

Rules:

- The authority board supplies the output board setup, copper-layer set, thickness, and any project
  rules KiKit inherits.
- `reference_only: false` requires the authority's canonical path to match at least one board row.
- `reference_only: true` permits an empty/reference board that is not placed. It preserves the
  existing README workflow.
- A placed authority is still snapshotted once and reused for its board instances.
- Canonical matching uses `resolve(strict=True)` plus same-file identity where available. Case and
  symlink aliases cannot create two authorities or bypass collision checks.
- A placed authority is allowed—and required—to share source identity with one or more board rows.
  Duplicate board rows may share source identity while remaining distinct rows and instances. The
  output board and derived manifest must not alias any authority board, source board, authority
  companion, one another, or a transaction/staging path. Checks use canonical parent identity plus
  same-file identity where the target exists.
- Companion discovery is deterministic: `<stem>.kicad_pro` and `<stem>.kicad_dru` are consumed when
  present and included in hashes. Their absence is recorded, not fabricated. `.kicad_prl` and lock
  files are local state and are never authority inputs.

### 2.2 Layer count, stackup, and thickness

Version 1 implements this enforceable policy:

1. The authority copper-layer count must be greater than or equal to every source layer count.
   A lower-layer authority is always an error because it could drop source copper.
2. `allow_mixed_layers: false` requires every placed source copper-layer set and count to equal the
   authority. `allow_mixed_layers: true` permits a source whose copper set is a strict subset of the
   authority and whose count is no greater than the authority. Every such source and instance is an
   explicit coercion in the manifest. It never permits copper on an authority-absent layer.
3. Sources with the same copper-layer count as the authority but a different explicit descriptor
   are rejected in version 1. A boolean acknowledgement cannot waive this error.
4. KiCad 10's Python binding does not expose a traversable `BOARD_STACKUP` API. Parse the
   snapshotted board's `(setup (stackup ...))` S-expression and cross-check presence against
   `BOARD_DESIGN_SETTINGS.m_HasStackup`. Normalize ordered layer name/type/thickness/material/color,
   epsilon-r, and loss-tangent fields plus every global field, including copper finish and
   dielectric constraints. Lengths use integer IU; other numerics use canonical decimal strings.
   Unknown, malformed, or incomplete explicit fields yield `STACKUP_UNVERIFIED` and cannot prove
   equality.
5. `allow_mixed_thickness: false` requires every placed source thickness to equal the authority.
   `allow_mixed_thickness: true` permits differences and records every changed source and instance.
   Final thickness is always restored from and verified against the authority.
6. Source copper on a layer not present in the authority is an error even if reported counts appear
   compatible.

Tests cover a lower-layer authority, a maximum-layer authority, same-count/different-stackup
boards, missing descriptors, and mixed 0.6/1.0/1.6 mm sources.

### 2.3 Zones and DRC

Source zones are copied without refill. Zone outlines outside the packed copper extent emit
`ZONE_OUTSIDE_PACKED_EXTENT`. Generation and verification must not refill zones. Whole-panel DRC is
optional and reported separately; it is not a fabrication-readiness verdict for heterogeneous
source projects.

## 3. User experience

### 3.1 Desktop workflow

The main window contains:

1. **Board table:** add/drop `.kicad_pcb` files; path, quantity, margin, measured dimensions, layer
   count, thickness, planned rotation, remove, and reorder.
2. **Authority:** board picker, placed/reference-only toggle, final layer/thickness summary, explicit
   coercion acknowledgements, and stackup diagnostics.
3. **Panel settings:** optional maximum dimensions; flat-edge/fixed tabs; owned tab parameters;
   none/mousebite cuts; owned mousebite parameters; mill radius.
4. **Preview:** the active immutable plan's placements, instance IDs, rotations, extents, panel
   bounds, and tab-connectivity warnings. A generation result is accepted only if its generation
   token still matches the visible project revision.
5. **Output:** Validate, Generate, Cancel, and Open in KiCad; structured state and logs.
6. **Project files:** versioned YAML load/save, Save As path rebasing, and unsaved-change prompts.

**Validate** performs parsing, source snapshots, inspection, compatibility checks, and planning. It
does not claim final tab/inventory verification, which requires staged KiKit generation.

An unlocked existing output prompts before replacement. Any KiCad lock is non-overridable. The UI
never offers to replace an actively locked project.

### 3.2 Project path behavior

- Paths loaded from YAML resolve from the YAML directory.
- An unsaved GUI project stores selected paths as canonical absolute paths in memory.
- Save serializes paths relative to the destination YAML where possible.
- Save As rebases strings so they continue to reference the same canonical targets.
- CLI `--main` and `--output` paths resolve from the process working directory because they are
  command-line arguments; their resolved values and bases are recorded in the manifest.
- Symlinks may be preserved for display, but collision/security checks and source identity use
  canonical paths.

### 3.3 CLI contract

`pack` prints a human-readable summary to stdout and warnings/errors to stderr. It reports resolved
source paths and hashes, metadata, authority, acknowledgements, final dimensions and inventories,
output artifact paths and hashes, and manifest path.

Exit codes are stable:

| Code | Meaning |
|---:|---|
| 0 | generation succeeded, and `--open` was not requested or succeeded |
| 2 | CLI or configuration error |
| 3 | source/preflight/compatibility error |
| 4 | planning impossible, cancelled, or workload limit exceeded |
| 5 | KiKit generation failure |
| 6 | verification or artifact-promotion failure; previous output restored |
| 7 | runtime/dependency error |
| 8 | generation succeeded but `--open` failed |
| 130 | SIGINT cancellation; previous output preserved |

`--open` runs only after successful promotion. An open failure does not roll back a valid generated
project, but returns 8 and is recorded in the manifest/log. No first-release `inspect` subcommand is
planned; inspection is part of `pack`, GUI validation, and `doctor`.

### 3.4 Doctor contract

`doctor [project.yaml]` reports discovered runtimes; selected runtime; KiCad/`pcbnew`, KiKit,
wxPython, rectangle-packer, PyYAML, and Shapely versions; import/capability probes; display
availability separately from wx importability; plugin and project/output permissions; `kicad-cli`;
and tested/untested status.

`doctor` must run when optional/native imports are missing. Its top-level import path uses only the
standard library. Every candidate is probed in an isolated subprocess with a timeout, bounded
stdout/stderr, and crash reporting. `--json` writes only a versioned JSON object to stdout; all
human diagnostics go to stderr. Exit 0 means usable, 1 means usable with warnings, and 7 means no
usable runtime.

## 4. Versioned configuration

```yaml
version: 1
panel:
  authority:
    board: ../../hw/controller/controller.kicad_pcb
    reference_only: false
  output: combined.kicad_pcb
  max_width_mm: null
  max_height_mm: null
  layout:
    horizontal_spacing_mm: 0
    vertical_spacing_mm: 0
    rotation_deg: 0       # reserved; must be exactly 0 in schema version 1
    rename_net: Board_{n}-{orig}
    rename_ref: "{orig}"
    bake_text: true
    bake_ref: false
  tabs:
    mode: flat-edge
    width_mm: 2
    horizontal_count: 1
    vertical_count: 1
    min_distance_mm: 0
  cuts:
    mode: none
    drill_mm: 0.5
    spacing_mm: 0.8
    offset_mm: 0
    prolong_mm: 0
  post:
    mill_radius_mm: 1
    origin: top-left
    refill_zones: false
  page:
    mode: inherit
  allow_mixed_layers: false
  allow_mixed_thickness: false
boards:
  - board: ../../hw/probe/probe.kicad_pcb
    qty: 1
    margin_mm: 1
```

All resolved KiKit settings are application-owned and versioned. The defaults above are pinned to
the tested KiKit 1.8.1 behavior rather than inherited silently. The owned raw preset is complete
even when fields are not exposed in YAML: version 1 explicitly disables framing, tooling,
fiducials, all text sections, copper fill, scripts, dimensions, debug drawings, arc reconstruction,
outer mill radius, and zone refill; pins tab fillet, source handling, page anchor/position, edge
width, and every other KiKit field; and stores a digest of the portable raw preset plus the child's
normalized processed preset. Flat-edge mode uses only `width_mm`; fixed mode uses width,
horizontal/vertical counts, and minimum distance. Non-default irrelevant fields are rejected.
Unsupported settings fail instead of falling through to KiKit defaults.

The raw path-loaded layout plugin remains compatible with legacy YAML and externally supplied
KiKit `--tabs`, `--cuts`, and `--post`. When given version 1 YAML, it compares external KiKit
settings to the project and fails on conflicts; it does not silently ignore project settings.

### 4.1 Strict validation

The loader rejects duplicate YAML keys, non-mapping/null roots, unsupported versions, unknown keys,
YAML 1.1 surprise booleans, booleans where integers are required, wrong scalar/container types,
non-finite numbers, empty board lists, wrong suffixes, and canonical path aliases. Constraints are
field-specific: quantities and tab width are positive; maxima are null or positive; padding,
spacing, offset, prolongation, minimum distance, and mill radius may be zero but not negative.
`panel.layout.rotation_deg` must be exactly zero in version 1; raw legacy KiKit invocation continues
to honor external rotation. Diagnostics contain stable code, severity, JSON-pointer-like path,
message, and offending value type without exposing secrets.

### 4.2 Legacy mapping

Legacy files are accepted without rewrite until explicitly saved:

| Legacy field | Version 1 mapping |
|---|---|
| `boards` | same rows and order; duplicate rows remain distinct |
| board `qty`, `margin_mm` | same meaning |
| board `rotate` | accepted but remains ineffective; `LEGACY_ROTATE_IGNORED` warning |
| `max_width`, `max_height` | `panel.max_width_mm`, `panel.max_height_mm` |
| `ignore_layer_count` | `allow_mixed_layers`; maximum-layer authority is still mandatory |
| `ignore_thickness` | `allow_mixed_thickness` |

Legacy CLI/raw-plugin authority remains the KiKit main input. New CLI use of a legacy file requires
`--main` unless a unique board can be selected only by an explicit future migration action; it is
never guessed. CLI overrides apply in memory, are listed in the manifest, and never mutate YAML.

## 5. Immutable run protocol

Preview, generation, verification, and manifest must refer to one immutable result.

### 5.1 Snapshot and plan

Validation creates a sibling staging directory on the same filesystem as the final output. It:

1. parses the project and assigns a monotonically increasing GUI generation token;
2. snapshots each unique board into `inputs/<source-id>/<original-basename>`. It copies
   `.kicad_pro` and `.kicad_dru` only beside the authority snapshot in a versioned run and records
   non-authority companions as present-but-ignored. A placed authority reuses this snapshot. Raw
   legacy mode retains original-path companion behavior;
3. hashes original and snapshot bytes and verifies equality;
4. inspects snapshots, not mutable originals;
5. expands rows into stable instance IDs (`row-0001/instance-0001`);
6. computes one `RunPlan` and writes `run-plan.json` atomically.

`RunPlan` has a schema version, random run ID/nonce, project digest, resolved settings, source and
companion hashes, instance order, integer source-area/copper/outline bounds, margins, rotations,
placements, panel bounds, evaluated rotation count, runtime, diagnostics, and expected transformed
inventory fingerprints.

Coordinates are signed integer KiCad internal units (1 IU = 1 nm). Bounds are half-open
`[left, top, right, bottom)`. Packing positions are the padded-rectangle top-left before KiKit page
post-processing. Rotation is clockwise `0` or `90` degrees about the source-area origin. The plan
records the exact `Origin.TopLeft`/`Origin.TopRight`, source-area expansion, and rounding operation
used by `appendBoard`. Final plugin-reported coordinates are a separate field after any KiKit page
translation.

Legacy inputs use a frozen `legacy_optimal_pack` until explicitly saved/migrated; successful
placements remain byte-for-byte compatible with the baseline algorithm. Version-1 inputs enumerate
rotation vectors in instance order and compare candidates by integer enclosing area, rotated
original area, rotation-bit vector (`false < true`), then placement vector. `margin_mm` remains the
baseline packing-inflation formula in version 1 and is labelled packing padding, not guaranteed
pairwise clearance. Empty input and impossible bounds produce structured errors, never empty tuples
or incidental exceptions.

### 5.2 Child generation and plugin execution contract

Versioned generation launches the selected runtime as an argument array equivalent to:

```text
python -m kikit_packer.plugin_child <run-contract>
```

The child validates the contract/plan, constructs the complete owned KiKit 1.8.1 preset, and calls
`kikit.panelize_ui.doPanelization` programmatically. It supplies the package layout plugin and
recorder hook as direct plugin tuples, avoiding KiKit 1.8.1's broken CLI hook-argument parser. Raw
legacy `kikit panelize` remains unchanged.

The layout plugin verifies schema, nonce, hashes, instances, and settings before the first append;
applies supplied placements without replanning; and records per-append UUID/inventory deltas. The
recorder hook captures substrates after layout, tab polygons after tabs, cut/NPTH deltas after cuts,
and final in-memory translation/inventories at finish. No callback is assumed after save. After
`doPanelization` returns, the child reloads and hashes saved artifacts and atomically writes
`plugin-result.json` with run ID and nonce.

All pinned/private behavior lives in `kikit_adapter.py`. It retains the cloned authority layer set
and stackup; temporarily sets only `panel.copperLayerCount` and board thickness to each source for
KiKit append checks; restores authority thickness and layer state after all appends; explicitly
imports reference-only authority custom rules; and asserts the final authority layer count, enabled
copper set, descriptor, and thickness. The child never opens original paths.

The parent rejects missing, malformed, mismatched, duplicate, or stale results. Structured events
use append-only staging `events.jsonl`, not stdout/stderr. Each event has schema version, run ID,
nonce, monotonic sequence, optional GUI generation token, stage, kind, and bounded JSON-safe
payload. The parent tails this file while independently draining bounded child stdout/stderr. Missing
events never replace final protocol validation.

### 5.3 Normative protocol shapes

Protocol JSON uses UTF-8, sorted keys, compact separators, and no binary floating-point values.
Lengths are integer IU; non-length decimals such as dielectric constants are canonical strings.
Rectangles are `[left, top, right, bottom]` half-open. Staging paths are root-relative,
traversal-free, and resolved below the contract's staging root.

A diagnostic is:

```json
{
  "code": "STACKUP_MISMATCH",
  "severity": "error",
  "path": "/boards/1/board",
  "message": "Explicit stackup differs from authority",
  "value_type": "string",
  "context": {}
}
```

Codes and paths are stable API; messages are not. `context` is bounded and JSON-safe.

`RunPlan` has this required top-level shape:

```json
{
  "kind": "kikit-packer.run-plan",
  "schema_version": 1,
  "run_id": "uuid",
  "nonce": "64-lowercase-hex",
  "project_digest": "sha256",
  "runtime": {},
  "authority": {
    "source_id": "source-0001",
    "reference_only": false,
    "board_sha256": "sha256",
    "companions": {
      "kicad_pro": {"present": true, "sha256": "sha256"},
      "kicad_dru": {"present": false, "sha256": null}
    }
  },
  "sources": [{
    "source_id": "source-0001",
    "original_path": "display-only path",
    "snapshot_path": "inputs/source-0001/board.kicad_pcb",
    "sha256": "sha256",
    "inspection": {}
  }],
  "instances": [{
    "instance_id": "row-0001/instance-0001",
    "row_id": "row-0001",
    "source_id": "source-0001",
    "ordinal": 1,
    "outline_bounds_iu": [0, 0, 100, 100],
    "copper_bounds_iu": [0, 0, 100, 100],
    "source_area_iu": [-1000000, -1000000, 1000100, 1000100],
    "packing_size_iu": [2000100, 2000100],
    "margin_iu": 1000000,
    "packing_rotation_deg": 0,
    "append": {"destination_iu": [0, 0], "origin": "top-left", "rotation_deg": 0},
    "expected_inventory": {"profile": "kicad10-kikit181-v1", "categories": {}}
  }],
  "packing": {
    "max_width_iu": null,
    "max_height_iu": null,
    "candidate_limit": 1048576,
    "candidate_count": 2,
    "evaluated_count": 2,
    "bounds_iu": [0, 0, 2000100, 2000100]
  },
  "resolved_settings": {
    "project": {},
    "kikit_raw_preset": {},
    "kikit_raw_preset_digest": "sha256"
  },
  "diagnostics": []
}
```

`project_digest` covers normalized config, canonical input identities/hashes, and resolved settings,
but excludes run ID, nonce, timestamps, durations, and display-only paths.

`RunContract`, the sole generated absolute argv path, is:

```json
{
  "kind": "kikit-packer.run-contract",
  "schema_version": 1,
  "run_id": "uuid",
  "nonce": "64-lowercase-hex",
  "staging_root": "/absolute/private/staging",
  "run_plan_path": "run-plan.json",
  "run_plan_sha256": "sha256",
  "staged_output": "artifacts/panel.kicad_pcb",
  "plugin_result_path": "plugin-result.json",
  "events_path": "events.jsonl",
  "log_limits": {"stdout_bytes": 1048576, "stderr_bytes": 1048576}
}
```

`PluginResult` contains kind/schema/run ID/nonce/plan hash; lifecycle booleans for layout, tabs,
cuts, and save; per-instance pre-page/final bounds, output UUIDs, and actual inventory; final
translation; tab polygon/material components, graph edges, and connected components; cut mode,
generated NPTH fingerprints and mousebite references; after-layout/before-save/saved inventories;
staged artifact paths/sizes/hashes; and diagnostics. It never contains the final parent-written
manifest.

Each event contains `kind: kikit-packer.event`, schema version, run ID, nonce, monotonic sequence,
optional GUI generation token, stage, event kind, and bounded payload. Codec tests reject duplicate
keys, wrong kind/version, malformed nonce/hash, stale IDs, noncanonical numeric values, digest
mismatch, path escapes, and oversized payloads.

### 5.4 Packing workload and cancellation

The current exhaustive rotation search remains the first implementation, with no NumPy dependency.
It runs in a worker process and checks cancellation between candidates. The default interactive
limit is 20 instances or 1,048,576 rotation candidates, configurable only by a documented CLI
`--max-rotation-candidates` override recorded in the manifest. Preview debounces edits by 250 ms.
Cancellation must be observed within one candidate plus 250 ms of scheduler delay. Phase 0 records
p50/p95 planning times for 8, 12, 16, and 20 instances; release thresholds are based on those
fixtures rather than the word “immediate.”

## 6. Inspection and verification

### 6.1 Preflight

Preflight returns structured diagnostics for file existence/loadability; closed non-empty outline;
outline, copper-aware, and padded extents; quantities/margins; layer set/count, explicit stackup,
and thickness; outlying zone outlines; panel maxima; tab-facing edges; path collisions; output
locks; and write permissions.

Stable codes include `MIXED_LAYER_COUNT`, `MIXED_THICKNESS`, `STACKUP_MISMATCH`,
`STACKUP_UNVERIFIED`, `ZONE_OUTSIDE_PACKED_EXTENT`, `NO_VALID_FLAT_EDGE_TAB`,
`OUTPUT_LOCKED`, `PLANNING_IMPOSSIBLE`, and `SOURCE_CHANGED`.

### 6.2 KiKit append profile and per-instance proof

Inspection separates (a) selected copy-preserved entities, (b) consumed KiKit annotations/cut
graphics, and (c) source substrate geometry. The versioned `kicad10-kikit181-v1` append profile
covers selected non-annotation footprints and nested pads/graphics, tracks/arcs/vias, zones, and
non-`Edge.Cuts` drawings. Source `Edge.Cuts` are substrate geometry because KiKit replaces them with
generated panel edges. Expected zone outlines are clipped to instance substrate geometry while
stored filled polygons must remain an exact transformed multiset.

Per-append output UUID deltas attribute copied entities to instance IDs; UUIDs are recorded but
excluded from semantic fingerprints. Fingerprints include type, layer, transformed geometry,
drill/plating, and other fabrication attributes. Tests expand the profile entity class by entity
class and mutate every covered field. Generated panel edges, tabs, and cuts are separate categories.
Every requested instance must have its profile and substrate proof, including boards without
footprints or tracks.

### 6.3 Cuts and connectivity

For `cuts.mode: none`, output NPTH fingerprints/counts must equal the transformed source NPTH
inventory. Existing source mounting holes are valid; the check is zero generated mousebite NPTH,
not zero total NPTH.

Tabs are verified from generated material, not individual half-tab polygons. The plugin unions
`panel.forwardTabs` into material components and maps each component to intersected instance
substrate polygons. A component touching exactly two instances contributes one undirected graph
edge; zero/one touched instance is invalid, and more than two is ambiguous and rejected in version
1. One instance is connected vacuously and does not invoke zero-tab failure. Multi-instance output
must form one connected component.

### 6.4 Final verification

Before promotion:

- staged board parses with `pcbnew`;
- authority layer set/count, thickness, and inherited setup match policy;
- per-instance and generated inventories pass;
- cut delta and tab graph pass;
- source and companion snapshots still match recorded hashes;
- plugin result matches the run plan and output hash;
- output was produced by the current nonce-bearing run;
- a temporary-copy refill leaves every zone-layer copper area exactly unchanged;
- zones in the promoted output were not refilled.

## 7. Output artifact transaction

The managed final artifact set is:

```text
<stem>.kicad_pcb
<stem>.kicad_pro      # when KiKit/authority produces it
<stem>.kicad_dru      # when KiKit/authority produces it
<stem>.kicad_pcb.panel.json
```

The manifest hashes the promoted board and companions but never itself. After promotion the CLI may
compute and print the manifest hash externally. The manifest is never rewritten for later
Open-in-KiCad results.

`.kicad_prl`, `~*.lck`, logs, run contracts, snapshots, and temporary files are never promoted.

Generation occurs in a unique hidden sibling staging directory so final replacement remains on one
filesystem. The runner removes staging lock/local-state files after the child exits, verifies the
managed set, writes and fsyncs the manifest in staging, then performs a rollback-capable transaction:

1. acquire an application transaction lock without overriding KiCad locks;
2. recheck `~<board-name>.lck`, `~<stem>.kicad_pro.lck`, and platform sharing errors immediately
   before mutation; any lock blocks, with no stale-lock override;
3. move every existing managed final artifact to a private backup directory;
4. move staged board and companions into place, then manifest last;
5. fsync files and containing directory where supported;
6. on any failure, remove partial new artifacts and restore every backup;
7. delete backups only after the complete set is durable.

Cancellation terminates the whole child process tree, drains pipes, waits for exit, and removes the
staging directory. It never touches final artifacts. Tests inject failures at generation, each
verification step, each promotion step, manifest write/fsync, cancellation, and rollback, and assert
byte preservation of the prior board, companions, and manifest.

## 8. Package architecture

```text
pyproject.toml
kikit-packer.py                 # path-loader bootstrap; exports Plugin/FlatEdgeTabs
kikit_packer/
  __init__.py                   # version only; no native imports
  __main__.py
  cli.py
  config.py
  model.py
  diagnostics.py
  inspect.py
  stackup.py
  fingerprint.py
  snapshot.py
  packing.py
  protocol.py
  kikit_adapter.py
  plugin.py
  plugin_child.py
  worker.py
  command.py
  runner.py
  verify.py
  artifacts.py
  manifest.py
  doctor.py
  runtime.py
  gui/
    __init__.py
    app.py
    frame.py
    board_table.py
    preview.py
    view_model.py
installer/
  install.py
  install-macos.sh
  install-linux.sh
  install-windows.ps1
  uninstall.py
tests/
  unit/
  integration/
  fixtures/
```

`kikit-packer.py` contains no panel logic. It resolves its own directory, temporarily makes that
checkout importable, imports package classes, and restores `sys.path` without assuming the current
working directory or an installed wheel. A clean-interpreter test invokes the tracked example from
an unrelated directory with the package absent from site-packages.

Native dependencies are imported only inside commands/functions that need them. `doctor`, config,
model, command construction, artifact transactions, and view-model tests remain importable when
`pcbnew`, KiKit, wx, or Shapely is absent.

## 9. Packaging and dependency policy

`pyproject.toml` uses a defined build backend, a single package version source, Python `>=3.9`, and
console script `kikit-packer = kikit_packer.cli:main`. Remove the trivial NumPy use rather than add a
direct dependency.

Direct runtime dependencies are constrained to tested compatible ranges:

- KiKit `>=1.8,<1.9` for the first release;
- rectangle-packer `>=2,<3`;
- PyYAML `>=6,<7`;
- Shapely `>=2,<3`.

`pcbnew` and wxPython are never pip dependencies; they come from the selected KiCad runtime. Test and
build extras pin pytest/build tooling separately. Release builds use a lock/constraints file with
hashes per supported runtime where wheel availability differs. Installers verify release artifact
SHA-256 before installation and run capability probes against the exact launcher environment.

A venv must not shadow a KiCad-shipped native package with an incompatible pip package. The
installer records provenance and rejects a resolution that replaces protected `pcbnew`, wx,
KiKit-native dependencies, Shapely, or NumPy contrary to the tested constraint set.

## 10. Installer and runtime design

### 10.1 Phase 0 feasibility gate

Before declaring a platform supported, test a clean machine for:

- KiCad interpreter discovery and version reporting;
- isolated `pcbnew` and wx imports;
- headless doctor behavior and interactive wx application startup;
- `venv --system-site-packages`, pip availability/bootstrap, and dependency installation;
- source-board load and staged KiKit generation;
- GUI launch from the installed desktop launcher;
- paths containing spaces and non-ASCII characters.

The initial local macOS result is evidence only for macOS arm64 with KiCad 10.0.5. Linux x86_64 and
Windows x86_64 remain provisional until their probes pass. KiCad 9/8 remain untested/unsupported
until their integration cells pass.

### 10.2 Discovery and install transaction

`installer/install.py` performs shared work:

1. discover candidates and probe each in isolated subprocesses;
2. select a supported runtime, prompting only in interactive mode;
3. download/select a release wheel and constraints, then verify hashes;
4. create a versioned staging environment with `--system-site-packages`;
5. install without replacing protected KiCad packages;
6. run `doctor --json` and a board-load smoke test through the staged launcher;
7. stage CLI/desktop launchers and a receipt;
8. atomically switch a small `current` pointer/launcher to the staged version;
9. retain the previous environment until the new version is confirmed.

A failed first install leaves no launcher. A failed upgrade preserves the previous launcher and
environment. Re-running the same version is idempotent. The installer reports when `~/.local/bin`
is not on PATH and prints both the absolute launcher and the exact PATH amendment; desktop launch
must not depend on shell PATH.

Platform discovery/locations:

- **macOS:** probe standard and versioned KiCad applications; use the app's Python; data under
  `~/Library/Application Support/KiKit Packer/`, CLI under `~/.local/bin/`, app under
  `~/Applications/KiKit Packer.app`.
- **Linux:** probe candidate interpreters for capabilities; use XDG data/config locations; CLI under
  `~/.local/bin/`; desktop file under `~/.local/share/applications/`.
- **Windows:** probe registry and standard installation directories; install below
  `%LOCALAPPDATA%\KiKit Packer`; create Start Menu and command launchers using argument-safe APIs.

### 10.3 Receipt-bounded uninstall

The receipt has a schema version, install ID, environment/launcher paths, package versions, file
hashes, allowed-root IDs, and receipt hash. Uninstall rejects corrupt receipts, paths outside known
install roots, symlink/reparse-point escapes, traversal, and files whose ownership/hash no longer
matches when removal would be unsafe. Dry-run is default unless `--yes` is supplied. It never
removes KiCad, user YAML, generated panels, or unrelated packages. Upgrade cleanup removes an old
environment only after it is no longer current.

## 11. GUI execution model

`view_model.py` owns project state, revision/generation tokens, validation results, button state,
and structured events without importing wx. wx modules own rendering and events only. The promoted
generation manifest is immutable and ends at promotion; later Open-in-KiCad results appear only in
CLI/UI logs and exit status.

Planning and generation run outside the UI process/thread that owns widgets. Events are JSON-safe
values posted through a bounded queue. The runner drains child stdout/stderr concurrently, emits
stage-level progress rather than invented KiKit percentages, and kills the process tree on cancel.
The UI states are `dirty`, `validating`, `planned`, `generating`, `verifying`, `promoting`,
`succeeded`, `failed`, and `cancelled`; allowed transitions are unit-tested.

Open in KiCad uses platform argument arrays (`open -a`, direct executable, or Windows shell API as
appropriate), never a shell command string.

## 12. Fixtures, tests, and CI

### 12.1 Fixture policy

Add narrow `.gitignore` exceptions under `tests/fixtures/**` for `.kicad_pcb`, `.kicad_pro`, and
`.kicad_dru`; continue ignoring generated boards elsewhere. Track documented KiCad 10 fixtures for:

- rectangular and necked/non-rectangular outlines;
- overhanging pad copper and outlying zone outlines;
- two-/four-layer and explicit differing stackups;
- 0.6/1.0/1.6 mm thicknesses;
- legitimate NPTH mounting holes;
- footprints, tracks, vias, zones, drawings, and project rules;
- single-board, connected multi-board, and disconnected-cluster tab cases;
- no-cuts and mousebite outputs.

Fixture metadata records KiCad version, generation steps, intended invariant, and license/source.
Semantic goldens contain normalized plans, inventories, diagnostics, and dimensions, not board byte
hashes.

### 12.2 Unit coverage

- strict YAML matrix, duplicate keys, versions, defaults, paths, Save As, legacy mapping;
- authority selection/canonical aliases/reference-only behavior and collisions;
- layer/stackup/thickness policy and acknowledgements;
- deterministic packing, units/origins/ties, empty/impossible/limit/cancel cases;
- immutable protocol validation, nonce/hash/stale-worker rejection;
- command argv for all owned settings, shell metacharacters, spaces, and conflicts;
- fingerprint transforms, NPTH deltas, generated classification, connectivity graph;
- transaction success and failure injection at every boundary;
- doctor with each dependency missing, hanging, crashing, or headless;
- view-model state transitions and stale event rejection;
- runtime discovery and installer receipt/path validation.

### 12.3 KiCad integration coverage

- source-checkout raw plugin from an external working directory;
- legacy and package plugin semantic equivalence;
- supplied placements equal actual plugin placements/final translation;
- authority setup/layers/thickness and companion artifacts are correct;
- every instance fingerprint is preserved;
- valid source NPTH holes survive and no-cuts adds none;
- mousebite delta and tab graph are correct;
- impossible/disconnected tabs fail before promotion;
- source mutation cannot alter a snapshot-bound run;
- failure/cancel/lock preserves every previous artifact;
- zones are not refilled.

### 12.4 GUI and installer coverage

GUI tests create/destroy the frame, edit/reorder/drop rows, focus invalid fields, exercise project
round trips, verify buttons/state/cancel, render plan data, and smoke test under Xvfb plus interactive
release candidates.

Installer tests cover zero/one/multiple runtimes, missing capabilities, paths with spaces, staged
first install, failed upgrade rollback, PATH warning, corrupt/malicious receipts, dry-run/uninstall,
and no-admin operation.

### 12.5 CI and release evidence

Establish lint/unit/build CI in Phase 0/1, not after the GUI. Document exact KiCad/KiKit provisioning
for every runner. Use hosted runners where packages are reproducible and self-hosted signed runners
for interactive/macOS/Windows cells that cannot be provisioned reliably. Archive doctor JSON,
integration summaries, installer logs, GUI smoke evidence, artifact hashes, and runner image/version
for each release matrix cell.

## 13. Implementation phases and gates

### Phase 0 — Preserve, qualify, and prove feasibility

1. Preserve the uncommitted baseline as a distinct diff; do not commit unless requested.
2. Add fixture ignore exceptions, representative fixtures, metadata, and semantic baseline tests.
3. Test current example output, mixed properties, FlatEdgeTabs, NPTH delta, and connectivity.
4. Characterize and pin KiKit API behavior: lifecycle order/no after-save callback, direct
   `doPanelization` plugin tuples, append layer/thickness resets, UUID persistence, companion reads,
   reference-only `.kicad_dru`, zone clipping/fills, and produced sidecars/locks.
5. Add initial CI and run the platform feasibility probes.
6. Record supported/provisional matrix cells and packing performance.

**Gate:** current behavior is covered, unsupported assumptions are explicit, and no relocation has
occurred.

### Phase 1 — Import-safe core and raw compatibility

1. Add packaging metadata, import-safe models/diagnostics, strict config, and protocol codecs.
2. Extract the frozen legacy planner exactly and prove successful-placement equivalence.
3. Move raw plugin logic and install the self-bootstrapping source-checkout shim.
4. Prove the tracked raw invocation from an external working directory remains equivalent.
5. Add the separate deterministic version-1 planner with exact objective, limits, and cancellation.
6. Implement stackup parsing, authority compatibility, snapshots, inspection, and the append-profile
   fingerprint primitives incrementally.

**Gate:** legacy users work from a clean checkout; pure versioned inputs can be validated, snapshotted,
and planned without changing plugin output.

### Phase 2 — Immutable generation, verification, transaction, and CLI

1. Compose immutable `RunPlan` creation from snapshots, inspection, compatibility, and packing.
2. Implement the complete owned preset, pinned `kikit_adapter.py`, supplied-plan layout plugin,
   recorder hook, and package-owned child generation.
3. Implement independent staged semantic verification and tab-material graph checks.
4. Implement the managed artifact transaction and failure-injection tests independently.
5. Combine events, bounded logs, cancellation, manifest, promotion, stable-exit `pack`, and `--open`.
6. Implement lazy runtime discovery and `doctor`.

**Gate:** one command produces a verified, source-bound project or preserves all previous artifacts.

### Phase 3 — View model and wx GUI

1. Implement/test view-model revisions, transitions, validation, and worker events.
2. Implement frame, board table/drop, authority/settings, preview, output, and logs.
3. Implement save/rebase, prompts, cancel, and platform-safe open.
4. Run headless and interactive smoke tests on currently supported cells.

**Gate:** a new user can create, validate, generate, and open without a terminal, and stale work
cannot replace current state.

### Phase 4 — Transactional installers

1. Implement discovery/probes and staged install/upgrade core.
2. Implement receipt validation and uninstall.
3. Add macOS launcher/bootstrap, then Linux and Windows separately after feasibility gates.
4. Exercise spaces/non-ASCII, multiple KiCad versions, failure rollback, and PATH behavior.

**Gate:** every declared-supported clean machine launches the same tested environment without admin
access or manual interpreter selection.

### Phase 5 — Release hardening

1. Complete supported matrix CI and archived smoke evidence.
2. Build wheel/sdist and signed or checksum-verified bootstrap artifacts.
3. Replace setup docs with installer-first instructions while retaining advanced legacy usage.
4. Document troubleshooting, manifest schema, exits, locks, rollback, and uninstall.
5. Record the GUI golden-path capture.

**Gate:** released artifacts, documentation, hashes, and support claims match archived evidence.

## 14. Acceptance criteria

1. Existing raw `kikit panelize ...kikit-packer.py.Plugin...` usage works from a source checkout
   without an installed package.
2. A supported user installs and launches the GUI without finding KiCad's Python manually.
3. Preview and generation use the same nonce/hash-bound `RunPlan`; stale results are rejected.
4. Authority can be placed or explicit reference-only, and output setup is source-bound.
5. A lower-layer authority and same-count/different-explicit-stackup source are rejected.
6. Allowed layer/thickness coercions are explicit and recorded.
7. No-cuts adds no NPTH beyond transformed source NPTH; mousebite additions are classified.
8. Every requested instance passes transformed fingerprint proof.
9. Multi-board tabs form one connected graph; a one-board panel is valid.
10. Failed, cancelled, locked, stale, or partially promoted runs preserve the complete previous
    managed artifact set.
11. Every successful run emits `<output>.panel.json` with source/companion hashes, settings, plan,
    actual plugin result, inventories, verification, artifacts, tool versions, and warnings.
12. Doctor remains useful with broken imports and has stable JSON/exit semantics.
13. GUI stays responsive during bounded planning/generation and cancellation kills the process tree.
14. Install/upgrade/uninstall are staged, rollback-capable, receipt-bounded, and do not modify KiCad
    or user projects.
15. Each claimed platform/version cell has archived integration, GUI, and installer evidence.

## 15. Non-goals

- replacing KiKit's panelization engine;
- editing source boards or refilling source zones;
- claiming heterogeneous-panel fabrication readiness from one inherited DRC map;
- automatic ordering/manufacturer submission;
- arbitrary V-score planning for non-aligned packed boards;
- bundling KiCad/`pcbnew` into an unrelated standalone runtime;
- supporting explicit incompatible stackup coercion in version 1;
- a full PCB editor.

## 16. Reviewable implementation boundaries

Keep behavior-complete changes independently reviewable:

1. Baseline regression fixtures and semantic goldens.
2. Package metadata, import-safe models, diagnostics, and strict config.
3. Pure packing and immutable run protocol.
4. Inspection, authority policy, snapshots, and fingerprints.
5. Behavior-preserving plugin move and source-checkout shim.
6. Supplied-plan plugin/result protocol and KiKit command construction.
7. Verification and tab/cut connectivity.
8. Artifact transaction and cancellation runner.
9. CLI and doctor.
10. GUI view model.
11. wx board/settings/preview UI.
12. wx runner/open/save integration.
13. Installer transaction and receipt core.
14. macOS launcher/bootstrap.
15. Linux launcher/bootstrap.
16. Windows launcher/bootstrap.
17. Matrix CI, release packaging, and documentation.

No commit is made unless explicitly requested. In particular, the existing 188-line behavior diff is
never hidden inside relocation or packaging churn.
