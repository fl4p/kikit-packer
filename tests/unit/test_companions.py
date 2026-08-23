import json
from pathlib import Path

import pytest

from kikit_packer.companions import (
    CompanionError,
    project_authority_profile,
    verify_project_authority,
)


def project(filename: str):
    return {
        "meta": {"filename": filename, "version": 1},
        "board": {"design_settings": {"rules": {"min_clearance": 0.2}}},
        "schematic": {"top_level_sheets": []},
        "net_settings": {
            "classes": [{"name": "Default", "clearance": 0.2}],
            "netclass_assignments": {},
        },
    }


def write(path: Path, value):
    path.write_text(json.dumps(value))


def test_output_local_project_changes_are_normalized_but_authority_is_preserved(tmp_path: Path):
    source = tmp_path / "source.kicad_pro"
    output = tmp_path / "panel.kicad_pro"
    source_value = project("source.kicad_pro")
    output_value = project("panel.kicad_pro")
    output_value["schematic"]["top_level_sheets"] = [{"filename": "panel.kicad_sch"}]
    output_value["net_settings"]["classes"].append({"name": "Board_0-Default", "clearance": 0.2})
    write(source, source_value)
    write(output, output_value)
    expected = project_authority_profile(source)
    verify_project_authority(output, expected)

    output_value["board"]["design_settings"]["rules"]["min_clearance"] = 0.3
    write(output, output_value)
    with pytest.raises(CompanionError, match="settings changed"):
        verify_project_authority(output, expected)
