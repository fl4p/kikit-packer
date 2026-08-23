# KiKit Packer

KiKit Packer combines multiple KiCad boards into a compact panel using KiKit and rectangle-packer.
It provides a versioned project format, a concise CLI, a wxPython desktop UI, immutable source
snapshots, output verification, and rollback-safe project promotion.

## Requirements

- KiCad 10.x with its Python `pcbnew` and wxPython modules
- KiKit 1.8.x
- Python 3.9 or newer from the KiCad-compatible runtime

The currently tested development environment is macOS arm64 with KiCad 10.0.5 and KiKit 1.8.1.
Other platform/version combinations remain provisional until their release smoke tests pass.

## Install from a source checkout

The tested macOS arm64/Python 3.9 bootstrap discovers KiCad's Python, creates a user-local
environment with `--system-site-packages`, installs a complete hash-locked dependency set without
build isolation, runs `doctor`, and creates `~/.local/bin/kikit-packer`:

```sh
./installer/install-macos.sh
```

A local source directory is treated as trusted development input. Release wheel installation also
requires its published SHA-256:

```sh
./installer/install-macos.sh --source dist/kikit_packer-0.1.0-py3-none-any.whl \
  --source-sha256 SHA256_FROM_RELEASE
```

`install-linux.sh` and `install-windows.ps1` deliberately fail closed until platform-specific,
hash-locked dependency sets pass their release gates. They are bootstrap entry points, not claims
of current platform support. No administrator privileges are required. The installer reports when
`~/.local/bin` is not on `PATH`. You can also use the existing development environment directly:

```sh
./venv-ki/bin/python -m kikit_packer doctor
```

## Project format

```yaml
version: 1
panel:
  authority:
    board: main.kicad_pcb
    reference_only: false
  output: combined.kicad_pcb
  max_width_mm: 100
  max_height_mm: 1000
  tabs:
    mode: flat-edge
    width_mm: 2
  cuts:
    mode: none
    drill_mm: 0.5
    spacing_mm: 0.8
    offset_mm: 0
    prolong_mm: 0
  post:
    mill_radius_mm: 1
    verify_refill_areas: true
  allow_mixed_layers: false
  allow_mixed_thickness: false
boards:
  - board: main.kicad_pcb
    qty: 1
    margin_mm: 2
  - board: long.kicad_pcb
    qty: 4
    margin_mm: 1
```

Project-relative paths resolve from the YAML directory. The authority supplies the final panel
layer set, thickness, and board setup. Set `reference_only: true` for an unplaced authority board.
A lower-layer authority is rejected. Mixed layer sets or thicknesses require their explicit
acknowledgements; incompatible explicit stackups are rejected.

`margin_mm` is packing padding retained for compatibility, not a guaranteed pairwise clearance.
The default cut mode is `none`; mousebite drilling is an explicit choice.

## CLI

Generate and optionally open a panel:

```sh
kikit-packer pack project.yaml
kikit-packer pack project.yaml --output other.kicad_pcb --open
```

Legacy YAML requires an explicit authority:

```sh
kikit-packer pack example/merge.yaml \
  --main example/main.kicad_pcb \
  --output combined.kicad_pcb
```

A successful run promotes this managed artifact set together:

```text
combined.kicad_pcb
combined.kicad_pro        # when produced
combined.kicad_dru        # when produced
combined.kicad_pcb.panel.json
```

Generation uses read-only snapshots and a nonce-bound plan. Source geometry and internal
`Edge.Cuts` are verified independently of derived zone fills. The experimental
`verify_refill_areas` guard defaults to `false`. When explicitly enabled, it requires every source
to be refill-stable, saves a canonically refilled staged panel, and independently refills a
temporary copy in the parent process. Exact integer area per zone-layer must remain identical;
equivalent filled-polygon representations are ignored. Failure, cancellation, verification errors,
and KiCad locks preserve the prior managed artifact set.

Diagnose runtime problems:

```sh
kikit-packer doctor
kikit-packer doctor project.yaml --json
```

## Desktop UI

```sh
kikit-packer gui [project.yaml]
```

The UI edits board rows and panel settings, validates and previews the immutable packing plan,
generates in a worker, supports cancellation, and opens the promoted board in KiCad.

## Legacy raw KiKit plugin

Path-loaded usage remains supported from a source checkout; installing the package is not required
for this command:

```sh
cd example
kikit panelize \
  --layout 'plugin; code: ../kikit-packer.py.Plugin; input:merge.yaml' \
  --tabs 'fixed; hwidth: 2mm; vwidth: 2mm' \
  --cuts 'mousebites' \
  --post 'millradiusouter: 1mm' \
  main.kicad_pcb combined.kicad_pcb
```

Legacy raw mode preserves KiKit's externally supplied tab, cut, post, rotation, and companion-file
behavior, but it does not run the temporary refill-area audit or atomic promotion checks. Use the
packaged `pack` command for those guards. The root `kikit-packer.py` file is only a checkout-relative
compatibility bootstrap; panel logic lives in `kikit_packer`.

## Uninstall

The installer prints its install root. Preview receipt-bounded removal first:

```sh
python3 installer/uninstall.py --root "$HOME/Library/Application Support/KiKit Packer"
```

Add `--yes` to remove only the receipt-recorded launchers and current or retained version
environments. A modified launcher or receipt aborts before any removal. User YAML files, generated
panels, and KiCad itself are never removed.

## Development

```sh
ruff check kikit_packer installer tests
pyright --project pyrightconfig.json
./venv-ki/bin/python -m pytest
./venv-ki/bin/python -m kikit_packer pack /path/to/project.yaml
```

`IMPLEMENTATION-PLAN.md` documents the run protocol, verification boundary, artifact transaction,
platform gates, and remaining release-hardening work.
