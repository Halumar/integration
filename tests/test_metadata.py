"""Static checks of the integration's metadata files.

Hassfest validates manifest, translations, icons and services.yaml, but
skips quality_scale.yaml for custom integrations; these tests keep it, the
translations and the icons consistent with the code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from custom_components.halumar.const import PLATFORMS
from custom_components.halumar.services import _ADMIN_SERVICES

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "halumar"

# Every rule of the quality scale (contributors.home-assistant.io, 11/2026).
RULES = {
    "bronze": [
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ],
    "silver": [
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ],
    "gold": [
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ],
    "platinum": ["async-dependency", "inject-websession", "strict-typing"],
}


def _json(name: str) -> dict[str, Any]:
    return json.loads((INTEGRATION / name).read_text(encoding="utf-8"))


def _leaves(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_leaves(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def test_quality_scale_lists_every_rule() -> None:
    """Every rule exactly once; exemptions and todos carry a reason."""
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    expected = [rule for tier in RULES.values() for rule in tier]
    assert len(expected) == 54
    assert sorted(rules) == sorted(expected)
    for name, value in rules.items():
        status = value if isinstance(value, str) else value["status"]
        assert status in ("done", "todo", "exempt"), name
        if status != "done":
            assert isinstance(value, dict) and value.get("comment"), name


def test_bronze_silver_and_gold_are_done_or_exempt() -> None:
    """No rule up to platinum is open (brand artwork shipped in brand/)."""
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    todo = [
        name
        for tier in ("bronze", "silver", "gold", "platinum")
        for name in RULES[tier]
        if (rules[name] if isinstance(rules[name], str) else rules[name]["status"])
        == "todo"
    ]
    assert todo == []


def test_strings_and_translations_match() -> None:
    """strings.json is en.json; de.json has every key with the same placeholders."""
    strings = _json("strings.json")
    assert _json("translations/en.json") == strings
    english = _leaves(strings)
    german = _leaves(_json("translations/de.json"))
    assert set(german) == set(english)
    for key, text in english.items():
        assert set(re.findall(r"\{(\w+)\}", text)) == set(
            re.findall(r"\{(\w+)\}", german[key])
        ), key


def test_actions_are_described_and_have_icons() -> None:
    """services.yaml, translations and icons cover exactly the registered actions."""
    registered = {name for name, _, _ in _ADMIN_SERVICES} | {
        "ack_notifications",
        "force_refresh",
        "control",
        "apply_rules",
        "report_location",
    }
    services_yaml = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services_yaml) == registered
    assert set(_json("strings.json")["services"]) == registered
    assert set(_json("icons.json")["services"]) == registered
    for name, spec in services_yaml.items():
        fields = set((spec or {}).get("fields", {}))
        translated = set(_json("strings.json")["services"][name].get("fields", {}))
        assert fields == translated, name


def test_icons_belong_to_translated_entities() -> None:
    """Every icon translation key names an entity translation key."""
    entity_strings = _json("strings.json")["entity"]
    for platform, keys in _json("icons.json")["entity"].items():
        assert platform in PLATFORMS
        assert set(keys) <= set(entity_strings[platform]), platform


# ── CI workflows ────────────────────────────────────────────────────────────

GITHUB = Path(__file__).parent.parent / ".github"
GITHUB_HOSTED = re.compile(r"^(ubuntu|windows|macos)-[\w.-]+$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
STEP_REF = re.compile(r"\bsteps\.([\w-]+)\.")
NEEDS_REF = re.compile(r"\bneeds\.([\w-]+)\.")


def _workflow_dir() -> Path:
    """The public form of the workflows (a separate folder where it exists)."""
    public = GITHUB / "public" / "workflows"
    return public if public.is_dir() else GITHUB / "workflows"


def _workflows() -> dict[str, dict[str, Any]]:
    files = sorted(_workflow_dir().glob("*.y*ml"))
    assert files
    return {f.name: yaml.safe_load(f.read_text(encoding="utf-8")) for f in files}


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_workflows_run_on_github_hosted_runners() -> None:
    for name, workflow in _workflows().items():
        for job_id, job in workflow["jobs"].items():
            if "uses" in job:  # a called workflow brings its own runners
                continue
            runs_on = job.get("runs-on")
            assert isinstance(runs_on, str), (name, job_id, runs_on)
            assert GITHUB_HOSTED.match(runs_on), (name, job_id, runs_on)


def test_workflow_references_resolve() -> None:
    for name, workflow in _workflows().items():
        jobs = workflow["jobs"]
        for job_id, job in jobs.items():
            needs = _needs(job)
            assert set(needs) <= set(jobs), (name, job_id, needs)
            text = yaml.safe_dump(job)
            assert set(NEEDS_REF.findall(text)) <= set(needs), (name, job_id)
            step_ids = {step["id"] for step in job.get("steps", []) if "id" in step}
            assert set(STEP_REF.findall(text)) <= step_ids, (name, job_id)


def test_third_party_actions_are_pinned_by_commit() -> None:
    for name, workflow in _workflows().items():
        for job_id, job in workflow["jobs"].items():
            uses = [job["uses"]] if "uses" in job else []
            uses += [step["uses"] for step in job.get("steps", []) if "uses" in step]
            for ref in uses:
                if ref.startswith("./"):
                    continue
                assert PINNED.match(ref), (name, job_id, ref)


def test_workflow_permissions_are_minimal() -> None:
    for name, workflow in _workflows().items():
        assert "permissions" in workflow, name
        assert workflow["permissions"] not in ("write-all", None), name


def test_hacs_action_ignores_no_check() -> None:
    hacs = [
        step
        for workflow in _workflows().values()
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("hacs/action@")
    ]
    assert hacs
    for step in hacs:
        assert "ignore" not in (step.get("with") or {})
