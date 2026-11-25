"""Version-neutral imports from the Home Assistant test harness.

HA >= 2026.11 ships its own test harness as ``pytest-homeassistant``
(``pytest_homeassistant``); older releases are only covered by the
discontinued ``pytest-homeassistant-custom-component``. The CI matrix runs
both (see requirements_test.txt / requirements_test_min.txt), so tests import
harness helpers from here instead of from either package directly.
"""

from __future__ import annotations

try:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        MockUser,
        async_capture_events,
        async_fire_time_changed,
        mock_restore_cache,
    )
except ImportError:  # HA < 2026.11: pytest-homeassistant-custom-component
    from pytest_homeassistant_custom_component.common import (  # type: ignore[no-redef]
        MockConfigEntry,
        MockUser,
        async_capture_events,
        async_fire_time_changed,
        mock_restore_cache,
    )

__all__ = [
    "MockConfigEntry",
    "MockUser",
    "async_capture_events",
    "async_fire_time_changed",
    "mock_restore_cache",
]
