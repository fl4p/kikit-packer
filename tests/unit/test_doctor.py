import json
import subprocess
import sys


def test_doctor_json_contract():
    process = subprocess.run(
        [sys.executable, "-m", "kikit_packer", "doctor", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode in (0, 1, 7)
    value = json.loads(process.stdout)
    assert value["kind"] == "kikit-packer.doctor"
    assert value["schema_version"] == 1
