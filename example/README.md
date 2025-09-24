This example includes a main board `main.kicad_pcb` and a smaller board `long.kicad_pcb`.

We want to order 1x main and 4x long with our PCB manufacturer.
This is defined in  `merge.yaml`, where both board files are referenced.

Now we need to run `kikat` with `kikat-packer` as a plugin-in. See `panelize.sh` (Linux & macOS) or `panelize.bat` (
Windows) to see how to execute the `kikat` command.