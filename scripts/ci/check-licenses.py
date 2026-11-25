#!/usr/bin/env python3
"""License gate of the integration (Apache-2.0).

* LICENSE is the unchanged Apache License 2.0 text (GitHub/HACS detect it as Apache-2.0;
  the Marketplace catalog requires a license GitHub can identify).
* NOTICE lists every runtime requirement of manifest.json (today: none). A new
  requirement must be added to NOTICE with its license, and that license must be
  approved by the Open Source Initiative (catalog and core requirement).

  python scripts/ci/check-licenses.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt
APACHE_2_0 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def main() -> int:
    problems: list[str] = []
    if hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() != APACHE_2_0:
        problems.append("LICENSE is not the unchanged Apache License 2.0 text")

    notice = (ROOT / "NOTICE").read_text("utf-8")
    manifests = sorted(ROOT.glob("custom_components/*/manifest.json"))
    if not manifests:
        problems.append("no custom_components/*/manifest.json found")
    for manifest in manifests:
        reqs = json.loads(manifest.read_text("utf-8")).get("requirements", [])
        if not reqs and "Third-party components: none" not in notice:
            problems.append("NOTICE must state 'Third-party components: none'")
        for req in reqs:
            name = re.split(r"[\s<>=!~\[;]", req, maxsplit=1)[0]
            if name.lower() not in notice.lower():
                problems.append(
                    f"{manifest.relative_to(ROOT)}: requirement {name} missing in NOTICE"
                )
        if reqs and "Third-party components: none" in notice:
            problems.append("NOTICE says 'none' but manifest.json has requirements")

    for p in problems:
        print(f"::error::{p}")
    print("licenses: ok" if not problems else f"licenses: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
