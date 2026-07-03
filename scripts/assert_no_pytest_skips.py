#!/usr/bin/env python3
"""Fail when a dedicated pytest JUnit report contains skipped tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", help="Path to pytest --junitxml output.")
    args = parser.parse_args()

    path = Path(args.junit_xml)
    if not path.exists():
        print(f"pytest JUnit XML not found: {path}", file=sys.stderr)
        return 2

    root = ET.parse(path).getroot()
    skipped: list[str] = []
    for testcase in root.iter("testcase"):
        skip = testcase.find("skipped")
        if skip is None:
            continue
        name = testcase.attrib.get("name", "<unknown>")
        classname = testcase.attrib.get("classname", "<unknown>")
        reason = skip.attrib.get("message") or (skip.text or "").strip()
        skipped.append(f"{classname}::{name}: {reason}")

    if skipped:
        print("unexpected pytest skips found:", file=sys.stderr)
        for item in skipped:
            print(f"- {item}", file=sys.stderr)
        return 1

    print(f"no pytest skips found in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
