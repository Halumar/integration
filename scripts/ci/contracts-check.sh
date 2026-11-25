#!/usr/bin/env bash
# Contract gate for the integration.
#
#   1. The integration domain in manifest.json is the frozen namespace of the contract
#      (namespace.json values.ha_domain).
#   2. Contract data bundled with the integration is byte-identical to the
#      pinned contract: control allowlist and event taxonomy.
#   3. Generated models, if the integration carries them
#      (custom_components/<domain>/contracts), match the pinned contract.
#
# CONTRACTS_DIR points to a contract checkout (CI: .contracts or the submodule).
set -euo pipefail
cd "$(dirname "$0")/../.."
C="${CONTRACTS_DIR:-.contracts}"
PY="${PYTHON:-python3}"

echo "contract $(tr -d '[:space:]' < "$C/VERSION") from $C"

domain="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["values"]["ha_domain"]["value"])' "$C/namespace.json")"
pkg="custom_components/${domain}"
if [ ! -f "${pkg}/manifest.json" ]; then
  echo "::error::${pkg}/manifest.json missing: the integration folder must equal the contract domain '${domain}'."
  exit 1
fi
manifest_domain="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["domain"])' "${pkg}/manifest.json")"
if [ "${manifest_domain}" != "${domain}" ]; then
  echo "::error file=${pkg}/manifest.json::domain '${manifest_domain}' != contract ha_domain '${domain}'."
  exit 1
fi
echo "domain: ${domain} (manifest = contract)"

for pair in "data/control-allowlist.json:control_allowlist.json" "data/events.json:event_kinds.json"; do
  src="${pair%%:*}"
  dst="${pkg}/${pair##*:}"
  if ! cmp -s "$C/${src}" "${dst}"; then
    echo "::error file=${dst}::differs from the contract ${src}; copy it from the pinned contract."
    exit 1
  fi
  echo "data: ${dst} = contract ${src}"
done

if [ -e "${pkg}/contracts" ]; then
  "$PY" "$C/tools/codegen/generate.py" --lang python --out "${pkg}" --package contracts --check
fi
echo "contracts-check: ok"
