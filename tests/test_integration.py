"""End-to-end tests running the integration inside a test Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_manager.const import (
    CONF_ARCHIVE_INTERVAL_HOURS,
    CONF_ARCHIVE_PATH,
    CONF_ARCHIVE_RETENTION_DAYS,
    CONF_ARCHIVE_STREAM,
    CONF_REOLINK_ENTRY_ID,
    CONF_TRIGGER_ENTITIES,
    CONF_TRIGGER_SETTLE_SECONDS,
    DOMAIN,
    SERVICE_SYNC_RECORDINGS,
)

HOST_UNIQUE_ID = "aa:bb:cc:dd:ee:ff"  # see conftest
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
    await hass.async_block_till_done(wait_background_tasks=True)

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
    await hass.async_block_till_done(wait_background_tasks=True)

    assert (old / "clip.mp4").exists()
    assert not (tmp_path / ".reolink_manager_archive").exists()
    api.request_vod_files.assert_not_awaited()


async def test_service_refuses_while_a_pass_is_running(
    hass: HomeAssistant, manager_entry: MockConfigEntry, tmp_path: Path
) -> None:
    hass.config_entries.async_update_entry(manager_entry, options=_options(str(tmp_path / "archive")))
    await _setup(hass, manager_entry)
    manager_entry.runtime_data.archiver._running = True

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


# --- following the official Reolink entry ------------------------------------


async def test_reolink_reload_moves_everything_to_the_new_host(
    hass: HomeAssistant,
    reolink_entry: MockConfigEntry,
    manager_entry: MockConfigEntry,
    api: MagicMock,
    new_reolink_runtime,
) -> None:
    """Changing the Reolink entry's options reloads it with a brand-new host:
    the switches must go unavailable meanwhile, then talk to the new api."""
    await _setup(hass, manager_entry)
    old_host = reolink_entry.runtime_data.host

    reolink_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    await hass.async_block_till_done()
    assert hass.states.get(MOTION).state == "unavailable"

    reolink_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    await hass.async_block_till_done()
    assert manager_entry.state is ConfigEntryState.LOADED  # waits, no pointless reload
    assert hass.states.get(MOTION).state == "unavailable"

    new_api = new_reolink_runtime({"MD": "1" * 168, "AI_ANIMAL": "1" * 168})
    reolink_entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    assert manager_entry.state is ConfigEntryState.LOADED
    assert manager_entry.runtime_data.host is reolink_entry.runtime_data.host
    assert manager_entry.runtime_data.host is not old_host
    assert hass.states.get(ANIMAL).state == "on"  # read from the new host's cache
    old_host.async_unregister_update_cmd.assert_any_call("GetRec", 0)

    await hass.services.async_call("switch", "turn_off", {"entity_id": MOTION}, blocking=True)
    new_api.send_setting.assert_awaited_once()
    api.send_setting.assert_not_awaited()


async def test_reolink_unload_stops_a_running_archive_pass(
    hass: HomeAssistant, reolink_entry: MockConfigEntry, manager_entry: MockConfigEntry, api: MagicMock, tmp_path: Path
) -> None:
    started = asyncio.Event()

    async def _hang(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(3600)

    api.request_vod_files = AsyncMock(side_effect=_hang)
    hass.config_entries.async_update_entry(manager_entry, options=_options(str(tmp_path / "archive")))
    await _setup(hass, manager_entry)
    await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)
    await asyncio.wait_for(started.wait(), 5)
    task = manager_entry.runtime_data.sync_task

    reolink_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    await hass.async_block_till_done()

    assert task.cancelled()
    assert manager_entry.runtime_data.archiver.running is False
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)


async def test_removed_reolink_entry_leaves_manager_waiting(
    hass: HomeAssistant, reolink_entry: MockConfigEntry, manager_entry: MockConfigEntry
) -> None:
    await _setup(hass, manager_entry)

    await hass.config_entries.async_remove(reolink_entry.entry_id)
    await hass.async_block_till_done()

    assert manager_entry.state is ConfigEntryState.SETUP_RETRY


# --- schedule refresh through the official coordinator --------------------------


async def test_switches_follow_the_official_coordinator_refresh(
    hass: HomeAssistant, reolink_entry: MockConfigEntry, manager_entry: MockConfigEntry, api: MagicMock
) -> None:
    """GetRec is only polled for registered channels; the switches register
    it themselves and are written on every coordinator refresh."""
    await _setup(hass, manager_entry)
    host = reolink_entry.runtime_data.host
    host.async_register_update_cmd.assert_any_call("GetRec", 0)
    assert hass.states.get(ANIMAL).state == "off"

    # Changed from the Reolink app; the next official poll re-reads GetRecV20.
    api._recording_settings[0]["schedule"]["table"]["AI_ANIMAL"] = "1" * 168
    await reolink_entry.runtime_data.device_coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(ANIMAL).state == "on"


# --- entity identity -----------------------------------------------------------


async def test_old_entry_based_unique_ids_are_migrated_keeping_entity_ids(
    hass: HomeAssistant, manager_entry: MockConfigEntry
) -> None:
    """0.1.x keyed unique_ids on the config entry id; upgrading must keep the
    existing entity_id (and so its history) instead of creating a duplicate."""
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{manager_entry.entry_id}_0_MD_recording",
        config_entry=manager_entry,
        suggested_object_id="garden_motion_rec",
    )
    assert old.entity_id == "switch.garden_motion_rec"

    await _setup(hass, manager_entry)

    migrated = registry.async_get("switch.garden_motion_rec")
    assert migrated.unique_id == f"{HOST_UNIQUE_ID}_0_MD_recording"
    assert hass.states.get("switch.garden_motion_rec").state == "on"
    assert hass.states.get(MOTION) is None  # no duplicate created
    assert len(er.async_entries_for_config_entry(registry, manager_entry.entry_id)) == 2


async def test_unique_ids_survive_recreating_the_manager_entry(
    hass: HomeAssistant, reolink_entry: MockConfigEntry, manager_entry: MockConfigEntry
) -> None:
    await _setup(hass, manager_entry)
    registry = er.async_get(hass)
    before = {e.unique_id for e in er.async_entries_for_config_entry(registry, manager_entry.entry_id)}

    await hass.config_entries.async_remove(manager_entry.entry_id)
    await hass.async_block_till_done()
    again = MockConfigEntry(
        domain=DOMAIN,
        title="Front Door",
        unique_id=reolink_entry.entry_id,
        data=dict(manager_entry.data),
    )
    again.add_to_hass(hass)
    await _setup(hass, again)

    after = {e.unique_id for e in er.async_entries_for_config_entry(registry, again.entry_id)}
    assert after == before
    assert hass.states.get(MOTION).state == "on"


async def test_unknown_trigger_is_named_from_its_raw_key(hass: HomeAssistant, manager_entry: MockConfigEntry, api: MagicMock) -> None:
    api._recording_settings[0]["schedule"]["table"]["AI_OTHER"] = "0" * 168

    await _setup(hass, manager_entry)

    state = hass.states.get("switch.front_door_ai_other_recording")
    assert state is not None
    assert state.attributes["friendly_name"] == "Front Door Ai Other recording"


# --- config flow (user step) ------------------------------------------------------


async def test_config_flow_creates_entry_for_a_loaded_reolink_entry(
    hass: HomeAssistant, reolink_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"

    with patch("custom_components.reolink_manager.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REOLINK_ENTRY_ID: reolink_entry.entry_id}
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_REOLINK_ENTRY_ID: reolink_entry.entry_id}


async def test_config_flow_reports_an_entry_unloaded_since_the_form_was_shown(
    hass: HomeAssistant, reolink_entry: MockConfigEntry
) -> None:
    other = MockConfigEntry(domain="reolink", entry_id="reolink-entry-2", title="Garage")
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    other.mock_state(hass, ConfigEntryState.NOT_LOADED)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REOLINK_ENTRY_ID: "reolink-entry-2"})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "entry_not_loaded"}


# --- a complete archive pass ------------------------------------------------------


async def test_full_archive_pass_downloads_and_prunes(
    hass: HomeAssistant, manager_entry: MockConfigEntry, api: MagicMock, tmp_path: Path
) -> None:
    """Service -> background pass: lists the window, downloads what is new
    under <camera>/<date>/, and prunes copies older than the retention."""
    root = tmp_path / "archive"
    now = dt_util.now()
    start = now - timedelta(hours=2)
    vod = MagicMock()
    vod.start_time = start
    vod.end_time = start + timedelta(minutes=1)
    vod.triggers = []
    vod.file_name = "Mp4Record/clip.mp4"
    vod.size = 5
    vod.start_time_id = vod.end_time_id = "x"

    async def _request(_channel, day_start, day_end, stream=None):
        return [], [vod] if day_start <= start <= day_end else []

    async def _chunks(_size):
        yield b"video"

    download = MagicMock(length=5)
    download.stream.iter_chunked = _chunks
    api.request_vod_files = AsyncMock(side_effect=_request)
    api.download_vod = AsyncMock(return_value=download)

    hass.config_entries.async_update_entry(manager_entry, options=_options(str(root), **{CONF_ARCHIVE_RETENTION_DAYS: 2}))
    await _setup(hass, manager_entry)
    # A first pass creates the archive; then plant an expired copy in it.
    await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    expired = root / "front_door" / "2020-01-01"
    expired.mkdir(parents=True)
    (expired / "old.mp4").write_bytes(b"x")

    await hass.services.async_call(DOMAIN, SERVICE_SYNC_RECORDINGS, {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    archived = list((root / "front_door" / f"{start:%Y-%m-%d}").glob("*.mp4"))
    assert [p.read_bytes() for p in archived] == [b"video"]
    assert api.download_vod.await_count == 1  # second pass: already archived
    assert not expired.exists()
    assert api.request_vod_files.await_args.kwargs["stream"] == "sub"
