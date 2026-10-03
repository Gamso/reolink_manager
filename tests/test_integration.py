"""End-to-end tests running the integration inside a test Home Assistant."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_manager.const import (
    CONF_ARCHIVE_INTERVAL_HOURS,
    CONF_ARCHIVE_PATH,
    CONF_ARCHIVE_RETENTION_DAYS,
    CONF_ARCHIVE_STREAM,
    CONF_TRIGGER_ENTITIES,
    CONF_TRIGGER_SETTLE_SECONDS,
    DOMAIN,
    SERVICE_SYNC_RECORDINGS,
)

MOTION = "switch.front_door_motion_recording"
ANIMAL = "switch.front_door_animal_recording"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _options(path: str, **overrides) -> dict:
    options = {
        CONF_ARCHIVE_PATH: path,
        CONF_ARCHIVE_RETENTION_DAYS: 3,
        CONF_ARCHIVE_INTERVAL_HOURS: 2,
        CONF_ARCHIVE_STREAM: "sub",
        CONF_TRIGGER_ENTITIES: [],
        CONF_TRIGGER_SETTLE_SECONDS: 30,
    }
    options.update(overrides)
    return options


# --- setup / unload ---------------------------------------------------------


async def test_setup_creates_one_switch_per_trigger_and_unloads(
    hass: HomeAssistant, manager_entry: MockConfigEntry
) -> None:
    await _setup(hass, manager_entry)

    assert manager_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert len(er.async_entries_for_config_entry(registry, manager_entry.entry_id)) == 2
    assert hass.states.get(MOTION).state == "on"
    assert hass.states.get(ANIMAL).state == "off"

    assert await hass.config_entries.async_unload(manager_entry.entry_id)
    await hass.async_block_till_done()
    assert manager_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(MOTION).state == "unavailable"


async def test_setup_retries_while_reolink_entry_not_loaded(
    hass: HomeAssistant, reolink_entry: MockConfigEntry, manager_entry: MockConfigEntry
) -> None:
    reolink_entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    await hass.config_entries.async_setup(manager_entry.entry_id)
    await hass.async_block_till_done()

    assert manager_entry.state is ConfigEntryState.SETUP_RETRY


# --- switch platform ----------------------------------------------------------


async def test_turning_a_switch_off_sends_the_schedule(
    hass: HomeAssistant, manager_entry: MockConfigEntry, api: MagicMock
) -> None:
    await _setup(hass, manager_entry)

    await hass.services.async_call("switch", "turn_off", {"entity_id": MOTION}, blocking=True)

    api.send_setting.assert_awaited_once()
    body = api.send_setting.await_args.args[0]
    assert body[0]["cmd"] == "SetRecV20"
    assert body[0]["param"]["Rec"]["schedule"]["table"]["MD"] == "0" * 168


# --- service ------------------------------------------------------------------


async def test_service_without_archive_raises(hass: HomeAssistant, manager_entry: MockConfigEntry) -> None:
    await _setup(hass, manager_entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)


async def test_service_starts_an_archive_pass(
    hass: HomeAssistant, manager_entry: MockConfigEntry, api: MagicMock, tmp_path: Path
) -> None:
    hass.config_entries.async_update_entry(manager_entry, options=_options(str(tmp_path / "archive")))
    await _setup(hass, manager_entry)

    await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)
    await hass.async_block_till_done()

    assert api.request_vod_files.await_count >= 1
    assert (tmp_path / "archive" / ".reolink_manager_archive").exists()


async def test_service_pass_on_a_foreign_tree_deletes_nothing(
    hass: HomeAssistant, manager_entry: MockConfigEntry, api: MagicMock, tmp_path: Path
) -> None:
    """End to end: options point at a folder another exporter already fills
    (set directly, bypassing the options-flow check); the pass is skipped."""
    old = tmp_path / "front_door" / "2020-01-01"
    old.mkdir(parents=True)
    (old / "clip.mp4").write_bytes(b"x")
    hass.config_entries.async_update_entry(manager_entry, options=_options(str(tmp_path), **{CONF_ARCHIVE_RETENTION_DAYS: 1}))
    await _setup(hass, manager_entry)

    await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)
    await hass.async_block_till_done()

    assert (old / "clip.mp4").exists()
    assert not (tmp_path / ".reolink_manager_archive").exists()
    api.request_vod_files.assert_not_awaited()


async def test_service_refuses_while_a_pass_is_running(
    hass: HomeAssistant, manager_entry: MockConfigEntry, tmp_path: Path
) -> None:
    hass.config_entries.async_update_entry(manager_entry, options=_options(str(tmp_path / "archive")))
    await _setup(hass, manager_entry)
    hass.data[DOMAIN][manager_entry.entry_id]["archiver"]._running = True

    with pytest.raises(HomeAssistantError, match="already in progress"):
        await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)


# --- options flow -------------------------------------------------------------


async def test_options_flow_saves_archive_settings(
    hass: HomeAssistant, manager_entry: MockConfigEntry, tmp_path: Path
) -> None:
    await _setup(hass, manager_entry)

    result = await hass.config_entries.options.async_init(manager_entry.entry_id)
    assert result["type"] == "form"

    archive = tmp_path / "archive"
    result = await hass.config_entries.options.async_configure(result["flow_id"], _options(str(archive)))
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert manager_entry.options[CONF_ARCHIVE_PATH] == str(archive)
    assert manager_entry.options[CONF_ARCHIVE_STREAM] == "sub"
    # Saving the options reloads the entry, now with the archive enabled.
    assert manager_entry.state is ConfigEntryState.LOADED


async def test_options_flow_rejects_relative_path(hass: HomeAssistant, manager_entry: MockConfigEntry) -> None:
    await _setup(hass, manager_entry)

    result = await hass.config_entries.options.async_init(manager_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _options("relative/path"))

    assert result["type"] == "form"
    assert result["errors"] == {CONF_ARCHIVE_PATH: "path_not_absolute"}
