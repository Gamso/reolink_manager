"""Tests for the Reolink schedule-switch logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.reolink_manager.switch import (
    ReolinkScheduleSwitch,
    _device_identifier,
    _schedule_table,
)


def _api_with_table(channel: int, table: dict, *, api_version: int = 1) -> MagicMock:
    api = MagicMock()
    api._recording_settings = {
        channel: {"channel": channel, "schedule": {"enable": 1, "table": table}}
    }
    api.api_version = MagicMock(return_value=api_version)
    api.send_setting = AsyncMock()
    return api


def test_schedule_table_returns_cached_table() -> None:
    api = _api_with_table(0, {"MD": "1" * 168})
    assert _schedule_table(api, 0) == {"MD": "1" * 168}


def test_schedule_table_missing_channel_returns_empty() -> None:
    api = _api_with_table(0, {"MD": "1" * 168})
    assert _schedule_table(api, 1) == {}


def test_device_identifier_non_nvr_uses_host_unique_id() -> None:
    host = MagicMock(unique_id="host-1")
    api = MagicMock(is_nvr=False)
    assert _device_identifier(host, api, 0) == "host-1"


def test_device_identifier_nvr_with_uid() -> None:
    host = MagicMock(unique_id="nvr-1")
    api = MagicMock(is_nvr=True)
    api.supported = MagicMock(return_value=True)
    api.camera_uid = MagicMock(return_value="cam-uid-42")
    assert _device_identifier(host, api, 3) == "nvr-1_cam-uid-42"


def test_device_identifier_nvr_without_uid_falls_back_to_channel() -> None:
    host = MagicMock(unique_id="nvr-1")
    api = MagicMock(is_nvr=True)
    api.supported = MagicMock(return_value=False)
    assert _device_identifier(host, api, 3) == "nvr-1_ch3"


def _make_switch(api: MagicMock, channel: int, trigger_key: str) -> ReolinkScheduleSwitch:
    entry = MagicMock(entry_id="entry-1")
    api.is_nvr = False
    data = MagicMock()
    data.api = api
    data.host = MagicMock(unique_id="host-1")
    data.coordinator = MagicMock(last_update_success=True)
    data.is_current = MagicMock(return_value=True)
    switch = ReolinkScheduleSwitch(entry, data, channel, trigger_key)
    # The entity was never added to hass by a platform, so async_write_ha_state's
    # own writable-state check would reject it; that check isn't what these
    # tests are about, so stub it out and assert on it directly where it matters.
    switch.async_write_ha_state = MagicMock()
    return switch


def test_is_on_true_when_any_hour_set() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "0" * 100 + "1" + "0" * 67})
    switch = _make_switch(api, 0, "AI_ANIMAL")
    assert switch.is_on is True


def test_is_on_false_when_all_hours_clear() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "0" * 168})
    switch = _make_switch(api, 0, "AI_ANIMAL")
    assert switch.is_on is False


def test_available_false_when_trigger_key_disappears() -> None:
    api = _api_with_table(0, {"MD": "1" * 168})
    switch = _make_switch(api, 0, "AI_ANIMAL")
    assert switch.available is False


def _refreshing_send_setting(api: MagicMock) -> AsyncMock:
    """Mimic reolink_aio: once the camera accepts a Set, the Get is re-read into the cache."""

    async def _send(body):
        rec = body[0]["param"]["Rec"]
        api._recording_settings[0]["schedule"]["table"] = dict(rec["schedule"]["table"])

    return AsyncMock(side_effect=_send)


async def test_turn_on_sends_only_the_schedule_with_v20() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "0" * 168, "MD": "1" * 168}, api_version=1)
    api._recording_settings[0]["scheduleEnable"] = 0  # recording turned off in switch.record
    api.send_setting = _refreshing_send_setting(api)
    switch = _make_switch(api, 0, "AI_ANIMAL")

    await switch.async_turn_on()

    body = api.send_setting.call_args[0][0]
    assert body == [
        {
            "cmd": "SetRecV20",
            "action": 0,
            "param": {"Rec": {"schedule": {"channel": 0, "table": {"AI_ANIMAL": "1" * 168, "MD": "1" * 168}}}},
        }
    ]
    # The global switch owned by the official integration is left alone.
    assert api._recording_settings[0]["scheduleEnable"] == 0
    assert switch.is_on is True
    switch.async_write_ha_state.assert_called_once()


async def test_turn_off_never_touches_global_enable_flags() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "1" * 168}, api_version=1)
    api._recording_settings[0]["scheduleEnable"] = 0
    switch = _make_switch(api, 0, "AI_ANIMAL")

    await switch.async_turn_off()

    rec = api.send_setting.call_args[0][0][0]["param"]["Rec"]
    assert set(rec) == {"schedule"}
    assert set(rec["schedule"]) == {"channel", "table"}
    assert api._recording_settings[0]["scheduleEnable"] == 0


async def test_turn_off_uses_legacy_command_when_unversioned() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "1" * 168}, api_version=0)
    switch = _make_switch(api, 0, "AI_ANIMAL")

    await switch.async_turn_off()

    body = api.send_setting.call_args[0][0]
    assert body[0]["cmd"] == "SetRec"
    assert body[0]["param"]["Rec"]["schedule"]["table"]["AI_ANIMAL"] == "0" * 168


async def test_failed_write_leaves_the_shared_cache_untouched() -> None:
    api = _api_with_table(0, {"AI_ANIMAL": "1" * 168}, api_version=1)
    api.send_setting = AsyncMock(side_effect=OSError("camera unreachable"))
    switch = _make_switch(api, 0, "AI_ANIMAL")

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()

    assert api._recording_settings[0]["schedule"]["table"]["AI_ANIMAL"] == "1" * 168
    assert switch.is_on is True
    switch.async_write_ha_state.assert_not_called()


def test_legacy_single_bitstring_table_yields_no_trigger() -> None:
    api = MagicMock()
    api._recording_settings = {0: {"schedule": {"enable": 1, "table": "1" * 168}}}
    assert _schedule_table(api, 0) == {}


async def test_set_raises_when_trigger_key_no_longer_present() -> None:
    api = _api_with_table(0, {"MD": "1" * 168})
    switch = _make_switch(api, 0, "AI_ANIMAL")

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()


def test_unavailable_while_the_reolink_entry_is_not_current() -> None:
    api = _api_with_table(0, {"MD": "1" * 168})
    switch = _make_switch(api, 0, "MD")
    assert switch.available is True

    switch._data.is_current.return_value = False

    assert switch.available is False
