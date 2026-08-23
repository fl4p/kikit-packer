import argparse
import json
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("inspect", "verify"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.operation == "inspect":
        from dataclasses import asdict

        from .inspect import inspect_board

        print(json.dumps(asdict(inspect_board(args.path, "source-0001")), default=str, sort_keys=True))
        return 0
    from .verify import verify_output

    print(json.dumps(verify_output(args.path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
