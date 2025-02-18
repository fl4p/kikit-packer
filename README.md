# KiKit Packer

Merge multiple boards into a single file with minimum area to reduce costs when ordering with your PCB manufacturer.

* Works with rectangular shaped boards
* based on [rectangle-packer](https://github.com/Penlect/rectangle-packer)
* finds maximum density packing by offset and 90° rotation
* applies penalty for rotation (prefer layouts with least rotated area)

# Example Output

![img.png](example.webp)

Input: 2x MCU head + 5x PSU for [Fugu2](https://github.com/fl4p/Fugu2) solar charger

## How to use

Requirements:

* KiCad
* [KiKit](https://yaqwsx.github.io/KiKit/latest/installation/intro/)

```
git clone ...
```

Create a yaml file listing all your boards you want to combine and how many copies you need:

```yaml
- board: debug-probe/debug-probe.kicad_pcb
  qty: 1
  margin_mm: 2      # default = 1
- board: sensor.kicad_pcb
  qty: 4
```

Here we name this file `probe-and-4sensors.yaml`.
File names in the `.yaml` file are relative to the folder where the `.yaml` is stored.

Then run kikit from shell:

```shell
kikit panelize \
  --layout 'plugin; code: kikit-packer.py.Plugin; input:probe-and-4sensors.yaml' \
    --tabs 'fixed; hwidth: 2mm; vwidth: 2mm' \
    --cuts 'mousebites' \
    --post 'millradius: 1mm' \
  sensor.kicad_pcb combined.kicad_pcb
```

The main board command line argument (`sensor.kicad_pcb` here) and output file (`combined.kicad_pcb`) are relative to
the current working directory. Usually the main board file name is one of the the boards from the `.yaml`.
Or you can set this to an empty reference board that contains all the design constraints for your PCB manufacturer.
The output board will have the same board setup (DRC etc.) as the main board file.

## Install (macOS)

Create a new virtual environment based on the KiCad one and install requirements:

```
PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
${PYTHON} -m venv --system-site-packages venv-ki
./venv-ki/bin/pip3 install -r requirements.txt
```

## Install (Linux)

Test if you can import `pcbwnew`:

```
python3 -c "import pcbnew; print(pcbnew._pcbnew)"
```

Create a new virtual environment:

```
PYTHON=python3
${PYTHON} -m venv --system-site-packages venv
./venv/bin/pip3 install -r requirements.txt
```

## Install (Windows)

```
"C:\Program Files\KiCad\8.0\bin\python.exe" -m pip install -r requirements.txt
```








