#!/usr/bin/env python3
"""Fail CI when coverage.xml falls below explicit line/branch thresholds."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def _percent(value: str) -> float:
    return float(value) * 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "coverage_xml",
        nargs="?",
        default="coverage.xml",
        help="Path to coverage.py XML output.",
    )
    parser.add_argument(
        "--min-line",
        type=float,
        default=float(os.getenv("COVERAGE_MIN_LINE", "90")),
        help="Minimum total line coverage percentage.",
    )
    parser.add_argument(
        "--min-branch",
        type=float,
        default=float(os.getenv("COVERAGE_MIN_BRANCH", "74")),
        help="Minimum total branch coverage percentage.",
    )
    args = parser.parse_args()

    path = Path(args.coverage_xml)
    if not path.exists():
        print(f"coverage XML not found: {path}", file=sys.stderr)
        return 2

    root = ET.parse(path).getroot()
    line = _percent(root.attrib["line-rate"])
    branch = _percent(root.attrib["branch-rate"])
    print(
        f"coverage: line={line:.2f}% (min {args.min_line:.2f}%), "
        f"branch={branch:.2f}% (min {args.min_branch:.2f}%)"
    )

    failures: list[str] = []
    if line + 1e-9 < args.min_line:
        failures.append(f"line coverage {line:.2f}% < {args.min_line:.2f}%")
    if branch + 1e-9 < args.min_branch:
        failures.append(f"branch coverage {branch:.2f}% < {args.min_branch:.2f}%")
    if failures:
        print("coverage gate failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
