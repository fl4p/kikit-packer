from pathlib import Path

from kikit_packer.stackup import parse_stackup


def test_stackup_parses_and_flags_unknown_fields(tmp_path: Path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text('(kicad_pcb (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "core") (thickness 1.53) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)) (layer "B.Cu" (type "copper") (thickness 0.035)) (copper_finish "ENIG") (dielectric_constraints no))))')
    result = parse_stackup(board)
    assert result["present"] is True
    assert result["verified"] is True
    assert result["descriptor"]["layers"][0]["fields"]["thickness_iu"] == 35000
    board.write_text('(kicad_pcb (setup (stackup (mystery yes))))')
    assert parse_stackup(board)["verified"] is False
    board.write_text('(kicad_pcb (setup (stackup (layer "F.Cu"))))')
    assert parse_stackup(board)["verified"] is False
