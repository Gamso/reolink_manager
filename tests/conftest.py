"""Shared fixtures for the Home Assistant integration tests.

These tests run the integration inside a real (test) Home Assistant instance
via pytest-homeassistant-custom-component's `hass` fixture. The official
Reolink integration is never actually set up - it would try to reach a
camera - so a `reolink` config entry is put in the LOADED state by hand, with
a `runtime_data` that mimics `ReolinkData` (host, device coordinator) around a
fake `reolink_aio` `Host.api`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_manager.const import CONF_REOLINK_ENTRY_ID, DOMAIN

HOST_UNIQUE_ID = "aa:bb:cc:dd:ee:ff"
REOLINK_ENTRY_ID = "reolink-entry-1"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/reolink_manager."""
    yield


def make_api(table: dict[str, str] | None = None) -> MagicMock:
    """Return a fake reolink_aio Host.api for a single standalone camera."""
    api = MagicMock()
    api.channels = [0]
    api.stream_channels = [0]
    api.is_nvr = False
    api.camera_name = MagicMock(return_value="Front Door")
    api.api_version = MagicMock(return_value=1)
    api._recording_settings = {
        0: {
            "enable": 1,
            "scheduleEnable": 1,
            "overwrite": 1,
            "saveDay": 30,
            "schedule": {
                "channel": 0,
                "table": dict(table or {"MD": "1" * 168, "AI_ANIMAL": "0" * 168}),
            },
        }
    }
    api.get_state = AsyncMock()
    api.send_setting = AsyncMock()
    api.request_vod_files = AsyncMock(return_value=([], []))
    api.download_vod = AsyncMock()
    return api


@dataclass
class FakeReolinkData:
    """Stand-in for homeassistant.components.reolink.util.ReolinkData."""

    host: MagicMock
    device_coordinator: DataUpdateCoordinator


def attach_reolink_runtime(hass: HomeAssistant, reolink_entry: MockConfigEntry, api: MagicMock) -> FakeReolinkData:
    """(Re)attach a fresh host/coordinator to the reolink entry, as a real reload does."""
    host = MagicMock()
    host.unique_id = HOST_UNIQUE_ID
    host.api = api
    coordinator = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        config_entry=reolink_entry,
        name="reolink",
        update_method=AsyncMock(return_value=None),
    )
    data = FakeReolinkData(host=host, device_coordinator=coordinator)
    reolink_entry.runtime_data = data
    return data


@pytest.fixture
def api() -> MagicMock:
    """The fake camera API held by the official integration."""
    return make_api()


@pytest.fixture
def reolink_entry(hass: HomeAssistant, api: MagicMock) -> MockConfigEntry:
    """A LOADED official Reolink entry, with its camera device registered."""
    # Pretend the official integration (a manifest dependency) is set up, so
    # Home Assistant does not try to set it up for real.
    hass.config.components.add("reolink")
    entry = MockConfigEntry(domain="reolink", entry_id=REOLINK_ENTRY_ID, title="Front Door", unique_id=HOST_UNIQUE_ID)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    attach_reolink_runtime(hass, entry, api)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("reolink", HOST_UNIQUE_ID)},
        name="Front Door",
    )
    return entry


@pytest.fixture
def manager_entry(hass: HomeAssistant, reolink_entry: MockConfigEntry) -> MockConfigEntry:
    """A Reolink Manager entry pointed at `reolink_entry`, not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Front Door",
        unique_id=reolink_entry.entry_id,
        data={CONF_REOLINK_ENTRY_ID: reolink_entry.entry_id},
        options={},
    )
    entry.add_to_hass(hass)
    return entry
