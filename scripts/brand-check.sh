#!/usr/bin/env bash
# Brand layer (docs: contracts/README.md "Marke"): generated brand files are up to date
# with contracts/brand.json, and brand lint finds nothing.
#
#   scripts/brand-check.sh            check (CI)
#   scripts/brand-check.sh --write    regenerate the brand files
#
# CONTRACTS_DIR points to a checkout of halumar/contracts (default: .contracts, the CI checkout).
set -euo pipefail
cd "$(dirname "$0")/.."
C="${CONTRACTS_DIR:-.contracts}"
mode=(--check)
[ "${1:-}" = "--write" ] && mode=()

python3 "$C/tools/brand/brandgen.py" --brand "$C/brand.json" "${mode[@]+"${mode[@]}"}" \
  --emit python/brand.py=custom_components/halumar/brand.py \
  --patch-manifest custom_components/halumar/manifest.json \
  --patch-manifest hacs.json
python3 "$C/tools/brand/brandlint.py" .
