#!/usr/bin/env bash
# Run Home Assistant core's pylint plugin (pylint-home-assistant) on the
# integration and its tests.
#
# The plugin only understands core's layout (homeassistant/components/<domain>,
# tests/components/<domain>). This script builds an overlay of the installed
# homeassistant package with the integration added as a component and runs
# pylint there, so the plugin also checks the claims in quality_scale.yaml.
#
#   pip install pylint ./<core checkout>/packages/pylint-home-assistant
#   scripts/ci/pylint-ha.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
domain="$(python3 -c 'import json; print(json.load(open("custom_components/halumar/manifest.json"))["domain"])')"
ha_dir="$(python3 -c 'import homeassistant, os; print(os.path.dirname(homeassistant.__file__))')"
overlay="$(mktemp -d)"
trap 'rm -rf "$overlay"' EXIT

mkdir -p "$overlay/homeassistant/components" "$overlay/tests/components"
for entry in "$ha_dir"/*; do
  [ "$(basename "$entry")" = components ] || ln -s "$entry" "$overlay/homeassistant/"
done
for entry in "$ha_dir"/components/*; do
  ln -s "$entry" "$overlay/homeassistant/components/"
done
cp -R "custom_components/$domain" "$overlay/homeassistant/components/$domain"
cp -R tests "$overlay/tests/components/$domain"
touch "$overlay/tests/__init__.py" "$overlay/tests/components/__init__.py"

cd "$overlay"
# Integration: every check of the plugin. Tests: the test conventions that
# back quality-scale rules (config-flow-test-coverage).
status=0
PYTHONPATH="$overlay" python3 -m pylint --rcfile=/dev/null --load-plugins=pylint_home_assistant \
  --disable=all --enable="$(python3 - <<'PY'
import importlib, pkgutil, pylint_home_assistant.checkers as c
names = set()
for m in pkgutil.walk_packages(c.__path__, c.__name__ + "."):
    mod = importlib.import_module(m.name)
    for obj in vars(mod).values():
        msgs = getattr(obj, "msgs", None)
        if isinstance(msgs, dict):
            names.update(v[1] for v in msgs.values())
print(",".join(sorted(names)))
PY
)" "homeassistant/components/$domain" || status=$?
PYTHONPATH="$overlay" python3 -m pylint --rcfile=/dev/null --load-plugins=pylint_home_assistant \
  --disable=all \
  --enable=home-assistant-tests-config-flow-error-recovery,home-assistant-tests-config-flow-unique-id \
  "tests/components/$domain" || status=$?
exit "$status"
