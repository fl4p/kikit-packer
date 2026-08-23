from copy import deepcopy

from kikit_packer.runtime import _support_problems


def supported_result():
    return {
        "system": "Darwin",
        "machine": "arm64",
        "python_major_minor": "3.9",
        "implementation": "CPython",
        "kicad": "10.0.5",
        "modules": {
            "pcbnew": {"ok": True, "origin": "/Applications/KiCad/KiCad.app/pcbnew.py"},
            "wx": {"ok": True, "origin": "/Applications/KiCad/KiCad.app/wx/__init__.py"},
            "kikit": {"ok": True, "version": "1.8.1"},
            "rpack": {"ok": True, "version": "2.0.2"},
            "yaml": {"ok": True, "version": "6.0.1"},
            "shapely": {"ok": True, "version": "2.0.7"},
        },
        "capabilities": {"board_create": True, "board_thickness": True},
    }


def test_only_attested_runtime_cell_is_supported():
    assert _support_problems(supported_result()) == []
    value = deepcopy(supported_result())
    value["kicad"] = "8.0.0"
    assert any("unsupported kicad" in problem for problem in _support_problems(value))
    value = deepcopy(supported_result())
    value["modules"]["kikit"]["version"] = "1.9.0"
    assert any("unsupported kikit" in problem for problem in _support_problems(value))


def test_protected_native_module_origin_is_enforced():
    value = supported_result()
    value["modules"]["pcbnew"]["origin"] = "/tmp/site-packages/pcbnew.py"
    assert any("unexpected origin" in problem for problem in _support_problems(value))
