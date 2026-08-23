from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .protocol import file_sha256


def artifact_records(paths: Iterable[Path]) -> list:
    return [
        {
            "path": path.name,
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in paths
        if path.exists()
    ]


def build_manifest(
    project_digest: str,
    plan: dict[str, Any],
    result: dict[str, Any],
    artifacts: Iterable[Path],
) -> dict[str, Any]:
    return {
        "kind": "kikit-packer.manifest",
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kikit_packer_version": __version__,
        "project_digest": project_digest,
        "run_id": plan["run_id"],
        "runtime": plan.get("runtime", {}),
        "sources": plan.get("sources", []),
        "authority": plan.get("authority"),
        "instances": plan.get("instances", []),
        "packing": plan.get("packing", {}),
        "resolved_settings": plan.get("resolved_settings", {}),
        "plugin_result": {
            key: value for key, value in result.items() if key != "parent_verification"
        },
        "verification": result["parent_verification"],
        "artifacts": artifact_records(artifacts),
        "diagnostics": plan.get("diagnostics", []) + result.get("diagnostics", []),
    }
