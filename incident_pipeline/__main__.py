from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import generate_runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local pipeline run artifacts")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts"),
        help="Directory for the two saved runs (default: artifacts)",
    )
    args = parser.parse_args()
    for summary in generate_runs(args.output_dir):
        print(f"{summary['run_id']}: {summary['status']}")


if __name__ == "__main__":
    main()
