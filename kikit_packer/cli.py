from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .diagnostics import PackerError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kikit-packer")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    gui = commands.add_parser("gui", help="open the desktop application")
    gui.add_argument("project", nargs="?", type=Path)
    pack = commands.add_parser("pack", help="generate a panel from a project")
    pack.add_argument("project", type=Path)
    pack.add_argument("--main", type=Path)
    pack.add_argument("--output", type=Path)
    pack.add_argument("--max-rotation-candidates", type=int, default=1_048_576)
    pack.add_argument("--open", action="store_true", dest="open_after")
    doctor = commands.add_parser("doctor", help="diagnose KiCad runtime availability")
    doctor.add_argument("project", nargs="?", type=Path)
    doctor.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _pack(args) -> int:
    from .command import open_board
    from .config import load_project
    from .runner import execute_run

    if args.max_rotation_candidates <= 0:
        print("--max-rotation-candidates must be positive", file=sys.stderr)
        return 2
    project = load_project(args.project, args.main, args.output)
    result = execute_run(project, Path(sys.executable), args.max_rotation_candidates)
    plan = result["plan"]
    print(f"Project: {project.source_path}")
    for source in plan["sources"]:
        inspection = source["inspection"]
        print("Source: {} sha256={} layers={} thickness={}mm".format(
            source["original_path"],
            source["sha256"],
            inspection["copper_layer_count"],
            inspection["thickness_iu"] / 1_000_000,
        ))
    print("Authority: {}".format(plan["authority"]["source_id"]))
    print("Panel: {} x {} mm".format(
        plan["packing"]["bounds_iu"][2] / 1_000_000,
        plan["packing"]["bounds_iu"][3] / 1_000_000,
    ))
    for artifact in result["artifacts"]:
        print(f"Output: {artifact}")
    if args.open_after:
        output = project.panel.output
        assert output is not None
        try:
            opened = open_board(output)
        except (OSError, RuntimeError) as exc:
            print(f"panel generated, but opening it failed: {exc}", file=sys.stderr)
            return 8
        if opened != 0:
            print("panel generated, but opening it failed", file=sys.stderr)
            return 8
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .doctor import run

            return run(args.project, args.json_output)
        if args.command == "gui":
            from .gui.app import run

            return run(args.project)
        return _pack(args)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except PackerError as exc:
        if getattr(args, "json_output", False):
            print(json.dumps(exc.diagnostic.to_dict(), sort_keys=True), file=sys.stderr)
        else:
            print(f"{exc.diagnostic.code}: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        exit_code = getattr(exc, "exit_code", 7)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
