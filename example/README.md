This example combines one `main.kicad_pcb` and four `long.kicad_pcb` boards.

The versioned project is `project.yaml`:

```sh
kikit-packer pack project.yaml
```

Its synthetic zones pour to source-board edges and intentionally change when those edges become
panel tabs, so this fixture explicitly skips the refill-area audit. Production projects should
leave `post.verify_refill_areas` enabled.

`merge.yaml`, `panelize.sh`, and `panelize.bat` remain legacy compatibility examples that invoke the
path-loaded KiKit plugin directly.
