This example combines one `main.kicad_pcb` and four `long.kicad_pcb` boards.

The versioned project is `project.yaml`:

```sh
kikit-packer pack project.yaml
```

The versioned project explicitly opts into the experimental refill-area guard and uses zone-free
copies under `safe/`, so its source and second-refill checks remain deterministic. The legacy files
retain their synthetic edge-poured zones.

`merge.yaml`, `panelize.sh`, and `panelize.bat` remain legacy compatibility examples that invoke the
path-loaded KiKit plugin directly.
