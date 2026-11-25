#!/usr/bin/env python3
"""Coverage gate of the quality scale.

test-coverage (Silver): more than 95% of every module.
config-flow-test-coverage (Bronze): 100% of config_flow.py.

Usage: check-coverage.py coverage.json   (pytest --cov-report=json)
Prints a Markdown table; exits 1 when a module is below its threshold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MINIMUM = 95.0
FULL = {"config_flow.py"}


def main() -> int:
    report = json.loads(Path(sys.argv[1]).read_text())
    failed = []
    print("| Module | Lines | Covered | Required |")
    print("|---|---:|---:|---:|")
    for path, data in sorted(report["files"].items()):
        name = Path(path).name
        percent = data["summary"]["percent_covered"]
        required = 100.0 if name in FULL else MINIMUM
        ok = percent >= 100.0 if name in FULL else percent > MINIMUM
        mark = "" if ok else " (below)"
        print(
            f"| {path} | {data['summary']['num_statements']} | "
            f"{percent:.1f} %{mark} | {'' if name in FULL else '> '}{required:.0f} % |"
        )
        if not ok:
            failed.append(path)
    if failed:
        print(f"\nBelow the threshold: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
