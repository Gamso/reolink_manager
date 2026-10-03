"""Switch platform for Reolink Manager.

Each switch controls one recording-schedule trigger type (motion, person,
vehicle, animal, ...) for one channel, by editing the per-hour bitstring that
`SetRecV20`/`GetRecV20` use (see the Reolink HTTP API guide, `Rec.schedule.table`).
reolink_aio caches this table verbatim in `Host._recording_settings` but has no
public accessor for it - the official integration only ever reads/writes the
single `enable` flag. There's no clean API to build on, so this reads the
cached dict directly and writes through the already-public `send_setting()`.

The cached dict is shared with the official integration, so it is never
mutated here: a change is sent as a fresh `{"schedule": {"channel", "table"}}`
body - nothing else of the cached `Rec` (enable, scheduleEnable, overwrite,
saveDay, postRec...) is resent - and `send_setting()` re-reads `GetRecV20`
into the cache once the camera has accepted it. A failed write therefore
leaves the cache, and `is_on`, reflecting the camera.

Turning a switch on/off always writes a full week of 1s or 0s for that trigger,
never a partial schedule: that's the on/off control the user actually wants,
and mixed schedules set from the Reolink app collapse to "on" (`is_on` is True
if any hour is set) so a partial schedule doesn't look falsely off.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import SIGNAL_REOLINK_AVAILABILITY, TRIGGER_LABELS
from .data import ReolinkManagerConfigEntry, ReolinkManagerData

_LOGGER = logging.getLogger(__name__)

# Domain of the official integration entities are attached to, so the
# switches show up on the same HA device as the camera's own entities
# instead of spawning a duplicate device.
REOLINK_DOMAIN = "reolink"

# The official integration's update command for the recording settings, as
# registered by its own switch.record (`cmd_key="GetRec"`); reolink_aio sends
# GetRecV20 for it on firmware that supports it.
UPDATE_CMD = "GetRec"


def _schedule_table(api: Any, channel: int) -> dict[str, str]:
    """Return the cached per-trigger schedule table for a channel, if any.

    Only `GetRecV20` reports a per-trigger dict; the legacy `GetRec` table is
    a single bitstring with no trigger breakdown, which yields no switch.
    """
    params = api._recording_settings.get(channel, {})  # pylint: disable=protected-access
    table = params.get("schedule", {}).get("table")
    return table if isinstance(table, dict) else {}


def _device_identifier(host: Any, api: Any, channel: int) -> str:
    """Reproduce the official Reolink integration's device identifier for a channel.

    Mirrors `ReolinkChannelCoordinatorEntity` in homeassistant/components/reolink:
    a standalone camera is one device keyed on the host's unique_id, while an
    NVR gets one device per channel, keyed on the camera's UID when the channel
    reports one and on the channel number otherwise. Matching it exactly is
    what makes these switches land on the camera's existing device page rather
    than creating a second, half-empty device beside it.
    """
    if not api.is_nvr:
        return host.unique_id
    if api.supported(channel, "UID"):
        return f"{host.unique_id}_{api.camera_uid(channel)}"
    return f"{host.unique_id}_ch{channel}"


async def async_setup_entry(
    hass: HomeAssistant, entry: ReolinkManagerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Reolink Manager switches from a config entry."""
    data = entry.runtime_data
    api = data.api

    entities: list[ReolinkScheduleSwitch] = []
    for channel in api.channels:
        try:
            await api.get_state(cmd="GetRecV20", ch=channel)
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.exception(
                "Could not read the recording schedule for channel %s (%s)",
                channel,
                api.camera_name(channel),
            )
            continue

        table = _schedule_table(api, channel)
        if not table:
            _LOGGER.debug(
                "Camera '%s' (channel %s) does not expose a per-trigger recording schedule",
                api.camera_name(channel),
                channel,
            )
            continue

        for trigger_key in table:
            entities.append(ReolinkScheduleSwitch(entry, data, channel, trigger_key))

    async_add_entities(entities)


class ReolinkScheduleSwitch(CoordinatorEntity, SwitchEntity):
    """Toggle one recording-schedule trigger type on/off for a whole week.

    State comes from reolink_aio's GetRecV20 cache, refreshed by the official
    integration's device coordinator - but that coordinator only sends the
    commands some entity registered for: GetRec is registered by the
    official `switch.<camera>_record`, and only while that switch is enabled.
    So each switch registers GetRec for its channel itself, exactly like the
    official channel entities do, and is written whenever that coordinator
    refreshes. No polling and no request of our own: one more command in the
    official integration's existing batched poll.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, data: ReolinkManagerData, channel: int, trigger_key: str) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self._api = data.api
        self._channel = channel
        self._trigger_key = trigger_key
        self._availability_signal = SIGNAL_REOLINK_AVAILABILITY.format(entry.entry_id)

        label = TRIGGER_LABELS.get(trigger_key, trigger_key.replace("_", " ").title())
        self._attr_name = f"{label} recording"
        self._attr_unique_id = f"{entry.entry_id}_{channel}_{trigger_key}_recording"
        self._attr_device_info = DeviceInfo(
            identifiers={(REOLINK_DOMAIN, _device_identifier(data.host, data.api, channel))},
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._data.host.async_register_update_cmd(UPDATE_CMD, self._channel)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._availability_signal,
                self.async_write_ha_state,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        self._data.host.async_unregister_update_cmd(UPDATE_CMD, self._channel)
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._data.is_current()
            and self._trigger_key in _schedule_table(self._api, self._channel)
        )

    @property
    def is_on(self) -> bool:
        bits = _schedule_table(self._api, self._channel).get(self._trigger_key, "")
        return "1" in bits

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        table = _schedule_table(self._api, self._channel)
        if self._trigger_key not in table:
            raise HomeAssistantError(
                f"Camera on channel {self._channel} no longer exposes trigger '{self._trigger_key}'"
            )

        # A copy: the cached table belongs to reolink_aio (and so to the
        # official integration) and must only change once the camera agreed.
        new_table = dict(table)
        new_table[self._trigger_key] = ("1" if enabled else "0") * len(table[self._trigger_key])

        # Only the schedule: the global recording flags (`scheduleEnable`,
        # `enable`) belong to the official integration's switch.record and
        # are left as they are, so turning one trigger off never re-enables
        # recording the user had turned off there.
        cmd = "SetRecV20" if self._api.api_version("GetRec") >= 1 else "SetRec"
        body = [
            {
                "cmd": cmd,
                "action": 0,
                "param": {"Rec": {"schedule": {"channel": self._channel, "table": new_table}}},
            }
        ]
        try:
            # On success reolink_aio re-reads GetRecV20 into its cache.
            await self._api.send_setting(body)
        except Exception as err:  # pylint: disable=broad-exception-caught
            raise HomeAssistantError(
                f"Could not update the recording schedule of {self._api.camera_name(self._channel)}: {err}"
            ) from err
        self.async_write_ha_state()
