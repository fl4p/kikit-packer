from pathlib import Path

import pytest

from kikit_packer.config import load_project
from kikit_packer.diagnostics import PackerError

VALID = """
version: 1
panel:
  authority:
    board: main.kicad_pcb
    reference_only: false
  output: panel.kicad_pcb
  tabs:
    mode: flat-edge
    width_mm: 2
  cuts:
    mode: none
boards:
  - board: main.kicad_pcb
    qty: 1
    margin_mm: 1
"""


def test_version_one_paths_and_defaults(tmp_path: Path):
    (tmp_path / "main.kicad_pcb").write_text("board")
    project_file = tmp_path / "pack.yaml"
    project_file.write_text(VALID)
    project = load_project(project_file)
    assert project.panel.authority is not None
    assert project.panel.authority.board == (tmp_path / "main.kicad_pcb").resolve()
    assert project.panel.tabs.mode == "flat-edge"
    assert project.panel.cuts.mode == "none"
    assert project.panel.post.verify_refill_areas is False


def test_refill_area_verification_accepts_explicit_true(tmp_path: Path):
    (tmp_path / "main.kicad_pcb").write_text("board")
    project_file = tmp_path / "pack.yaml"
    project_file.write_text(VALID.replace("  cuts:\n    mode: none\n", "  cuts:\n    mode: none\n  post:\n    verify_refill_areas: true\n"))
    assert load_project(project_file).panel.post.verify_refill_areas is True


def test_refill_area_verification_accepts_explicit_false(tmp_path: Path):
    (tmp_path / "main.kicad_pcb").write_text("board")
    project_file = tmp_path / "pack.yaml"
    project_file.write_text(VALID.replace(
        "  cuts:\n    mode: none\n",
        "  cuts:\n    mode: none\n  post:\n    verify_refill_areas: false\n",
    ))
    assert load_project(project_file).panel.post.verify_refill_areas is False


def test_duplicate_key_is_rejected(tmp_path: Path):
    project_file = tmp_path / "pack.yaml"
    project_file.write_text("version: 1\nversion: 1\n")
    with pytest.raises(PackerError) as caught:
        load_project(project_file)
    assert caught.value.diagnostic.code == "YAML_ERROR"


@pytest.mark.parametrize("section", ["layout", "tabs", "cuts", "post", "page"])
def test_false_section_is_rejected(tmp_path: Path, section: str):
    (tmp_path / "main.kicad_pcb").write_text("board")
    project_file = tmp_path / "pack.yaml"
    text = VALID
    if section == "tabs":
        text = text.replace("  tabs:\n    mode: flat-edge\n    width_mm: 2\n", "  tabs: false\n")
    elif section == "cuts":
        text = text.replace("  cuts:\n    mode: none\n", "  cuts: false\n")
    else:
        text = text.replace("  tabs:\n", f"  {section}: false\n  tabs:\n")
    project_file.write_text(text)
    with pytest.raises(PackerError) as caught:
        load_project(project_file)
    assert caught.value.diagnostic.code == "INVALID_TYPE"


def test_yaml_surprise_boolean_is_not_accepted(tmp_path: Path):
    project_file = tmp_path / "pack.yaml"
    project_file.write_text(VALID.replace("reference_only: false", "reference_only: no"))
    with pytest.raises(PackerError) as caught:
        load_project(project_file)
    assert caught.value.diagnostic.code == "INVALID_TYPE"
