@echo off

set input_yaml=merge.yaml

kikit panelize \
  --layout "plugin; code: ../kikit-packer.py.Plugin; input:%input_yaml%" \
    --tabs "fixed; hwidth: 2mm; vwidth: 2mm" \
    --cuts "mousebites" \
    --post "millradius: 1mm" \
  main.kicad_pcb combined.kicad_pcb