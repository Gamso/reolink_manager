"""Reolink Manager.

Two things the official `reolink` integration doesn't do:

* per-trigger recording-schedule switches (motion / person / vehicle / animal
  / ...), which the camera supports but the integration only exposes as one
  global "Record" switch - see switch.py;
* a local archive of the camera's recordings, so they can be browsed off a
  local disk instead of streamed slowly from the camera - see vod_archive.py.

Neither opens its own connection to the camera. Both look up the `reolink`
config entry the user picked in the config flow, reach into its already-running
`ReolinkHost` (`entry.runtime_data.host`) and reuse its `reolink_aio` `Host.api`
object directly. Opening a second connection to the same camera is exactly what
the Reolink docs warn against (limited concurrent connections), so reuse is a
correctness requirement here, not an optimization.

That borrowed host only lives as long as the official entry's current setup:
reloading the Reolink entry stops it and creates a new one. This entry follows
that lifecycle (see `_track_reolink_entry`): while the Reolink entry is not
loaded the switches are unavailable and archive passes are stopped, and once it
is loaded again with a new host this entry reloads to pick it up - otherwise it
would keep talking through a stopped host, whose re-login opens precisely the
second camera session this design exists to avoid.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    ARCHIVE_INITIAL_DELAY_SECONDS,
    CONF_ARCHIVE_INTERVAL_HOURS,
    CONF_ARCHIVE_PATH,
    CONF_ARCHIVE_RETENTION_DAYS,
    CONF_ARCHIVE_STREAM,
    CONF_REOLINK_ENTRY_ID,
    CONF_TRIGGER_ENTITIES,
    CONF_TRIGGER_SETTLE_SECONDS,
    DEFAULT_ARCHIVE_INTERVAL_HOURS,
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_ARCHIVE_STREAM,
    DEFAULT_TRIGGER_SETTLE_SECONDS,
    DOMAIN,
    SERVICE_SYNC_RECORDINGS,
    SIGNAL_REOLINK_AVAILABILITY,
)
from .data import ReolinkManagerConfigEntry, ReolinkManagerData
from .vod_archive import VodArchiver

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SWITCH]

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SYNC_RECORDINGS_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


def _iter_archiver_entries(hass: HomeAssistant) -> list[ReolinkManagerConfigEntry]:
    """Return loaded entries that have a recording archive configured."""
    return [
        entry
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if entry.runtime_data.archiver is not None
    ]


def _build_archiver(hass: HomeAssistant, entry: ConfigEntry, api) -> VodArchiver | None:
    """Return a configured archiver, or None when no archive folder is set.

    There is no separate on/off switch for the archive: an empty path means
    off, any path means on. Wanting neither the archive nor the schedule
    switches is a reason to remove the Reolink Manager entry entirely, not to
    keep a "disabled" one around.
    """
    raw_path = entry.options.get(CONF_ARCHIVE_PATH, "").strip()
    if not raw_path:
        return None

    return VodArchiver(
        hass,
        api,
        root=Path(raw_path),
        retention_days=entry.options.get(
            CONF_ARCHIVE_RETENTION_DAYS, DEFAULT_ARCHIVE_RETENTION_DAYS
        ),
        stream=entry.options.get(CONF_ARCHIVE_STREAM, DEFAULT_ARCHIVE_STREAM),
    )


def _is_off_transition(old_state, new_state) -> bool:
    """Return True for a genuine 1 -> 0 transition worth reacting to.

    Requires an actual prior "on" state, not just "not on": a sensor that is
    unknown/unavailable at startup and later settles to "off" has not just
    finished a detection, so treating that as a trigger would fire a sync with
    nothing new for it to find.
    """
    if old_state is None or new_state is None:
        return False
    return old_state.state == "on" and new_state.state == "off"


def _register_recording_triggers(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start_recent_sync: CALLBACK_TYPE,
    trigger_entities: list[str],
    settle_seconds: float,
) -> None:
    """Sync recordings shortly after any watched detection sensor clears.

    One shared debounce timer covers every watched entity: each 1->0
    transition pushes the sync later rather than firing immediately, so
    several sensors clearing close together (motion, then the animal label a
    few seconds later) collapse into a single sync after the *last* one
    settles - instead of racing a sync against a recording the camera has not
    finished writing yet. `settle_seconds` should comfortably exceed the
    camera's own post-recording buffer.
    """
    cancel_pending: dict[str, CALLBACK_TYPE | None] = {"cancel": None}

    @callback
    def _fire(_now) -> None:
        cancel_pending["cancel"] = None
        # The recording that just finished is from today: no need to re-list
        # the whole retention window after every detection.
        start_recent_sync()

    @callback
    def _handle_state_change(event: Event) -> None:
        if not _is_off_transition(event.data.get("old_state"), event.data.get("new_state")):
            return
        if cancel_pending["cancel"] is not None:
            cancel_pending["cancel"]()
        _LOGGER.debug(
            "%s cleared; recording sync in %ss unless another watched entity clears first",
            event.data.get("entity_id"),
            settle_seconds,
        )
        cancel_pending["cancel"] = async_call_later(hass, settle_seconds, _fire)

    entry.async_on_unload(
        async_track_state_change_event(hass, trigger_entities, _handle_state_change)
    )

    @callback
    def _cancel_pending_on_unload() -> None:
        if cancel_pending["cancel"] is not None:
            cancel_pending["cancel"]()

    entry.async_on_unload(_cancel_pending_on_unload)


def _track_reolink_entry(hass: HomeAssistant, entry: ReolinkManagerConfigEntry) -> None:
    """Follow the official Reolink entry's lifecycle.

    * Reolink entry loaded again with a new host (its options changed, a
      reauth, a reconnect...): reload this entry, so everything is rebuilt
      on top of the new host and api.
    * Reolink entry removed: reload too; setup then waits (ConfigEntryNotReady).
    * Reolink entry unloading, failed or disabled: its host is stopped. Stop
      any archive pass and mark the switches unavailable, until it is back.
    """
    data = entry.runtime_data
    reolink_entry_id = data.reolink_entry.entry_id

    @callback
    def _on_config_entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if changed.entry_id != reolink_entry_id or data.is_current():
            return
        if change is ConfigEntryChange.REMOVED or changed.state is ConfigEntryState.LOADED:
            _LOGGER.info(
                "Reolink entry '%s' was reloaded or removed; reloading Reolink Manager entry %s",
                changed.title,
                entry.title,
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        _LOGGER.debug(
            "Reolink entry '%s' is %s; pausing Reolink Manager entry %s until it is loaded again",
            changed.title,
            changed.state,
            entry.title,
        )
        data.cancel_sync()
        async_dispatcher_send(hass, SIGNAL_REOLINK_AVAILABILITY.format(entry.entry_id))

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_config_entry_changed)
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services, once per Home Assistant instance."""

    async def sync_recordings(call: ServiceCall) -> None:
        """Start an archive pass now instead of waiting for the next interval."""
        entries = _iter_archiver_entries(hass)
        if not entries:
            raise HomeAssistantError(
                "No Reolink Manager entry has the recording archive enabled"
            )

        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id is not None:
            matches = [entry for entry in entries if entry.entry_id == entry_id]
            if not matches:
                raise HomeAssistantError(
                    f"No Reolink Manager entry with the recording archive enabled has id {entry_id}"
                )
            target = matches[0].runtime_data
        elif len(entries) > 1:
            raise HomeAssistantError(
                "Multiple Reolink Manager entries have the recording archive enabled; "
                f"specify {ATTR_CONFIG_ENTRY_ID} in the service call"
            )
        else:
            target = entries[0].runtime_data

        if not target.is_current():
            raise HomeAssistantError(
                "The Reolink integration entry this Reolink Manager entry uses is not loaded"
            )

        # Say so rather than reporting success for a call that does nothing:
        # overlapping passes are skipped by the archiver.
        if target.archiver.running:
            raise HomeAssistantError(
                "An archive pass is already in progress for this entry; it will "
                "pick up new recordings itself"
            )

        # A pass can run for a long time on a first catch-up, so it is started
        # in the background rather than making the service call block on it.
        target.start_sync()

    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_RECORDINGS, sync_recordings, schema=SYNC_RECORDINGS_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ReolinkManagerConfigEntry) -> bool:
    """Set up Reolink Manager from a config entry."""
    reolink_entry_id = entry.data[CONF_REOLINK_ENTRY_ID]
    reolink_entry = hass.config_entries.async_get_entry(reolink_entry_id)

    if reolink_entry is None:
        raise ConfigEntryNotReady(
            f"The Reolink integration entry {reolink_entry_id} no longer exists"
        )
    if reolink_entry.state is not ConfigEntryState.LOADED:
        raise ConfigEntryNotReady(
            f"Reolink integration entry '{reolink_entry.title}' is not loaded yet"
        )

    reolink_data = reolink_entry.runtime_data
    host = reolink_data.host
    api = host.api
    archiver = _build_archiver(hass, entry, api)

    data = ReolinkManagerData(
        reolink_entry=reolink_entry,
        host=host,
        api=api,
        coordinator=reolink_data.device_coordinator,
        archiver=archiver,
    )
    entry.runtime_data = data
    _track_reolink_entry(hass, entry)

    if archiver is not None:
        def _launch(recent_only: bool) -> None:
            if not data.is_current():
                _LOGGER.debug(
                    "Not starting an archive pass for %s: its Reolink entry is not loaded",
                    entry.title,
                )
                return
            data.sync_task = entry.async_create_background_task(
                hass,
                archiver.async_sync(recent_only=recent_only),
                f"{DOMAIN} archive sync {entry.entry_id}",
            )

        # @callback is required, not cosmetic: without it Home Assistant treats
        # this as a blocking function and runs it in an executor thread, and
        # async_create_background_task must run in the event loop. Off-loop it
        # creates a task the loop never adopts, which asyncio later reports as
        # "Task was destroyed but it is pending!".
        @callback
        def _start_sync(_now=None) -> None:
            """Kick off an archive pass as an entry-owned background task.

            Background tasks are cancelled when the entry unloads, so a long
            download can't outlive the integration; overlapping passes are
            dropped by the archiver's own guard.
            """
            _launch(recent_only=False)

        @callback
        def _start_recent_sync() -> None:
            """Like _start_sync, but only for today's recordings (detection trigger)."""
            _launch(recent_only=True)

        data.start_sync = _start_sync
        data.start_recent_sync = _start_recent_sync

        interval_hours = entry.options.get(
            CONF_ARCHIVE_INTERVAL_HOURS, DEFAULT_ARCHIVE_INTERVAL_HOURS
        )
        entry.async_on_unload(
            async_track_time_interval(hass, _start_sync, timedelta(hours=interval_hours))
        )
        entry.async_on_unload(
            async_call_later(hass, ARCHIVE_INITIAL_DELAY_SECONDS, _start_sync)
        )
        _LOGGER.info(
            "Recording archive enabled for %s: every %dh into %s, keeping %d day(s)",
            entry.title,
            interval_hours,
            entry.options.get(CONF_ARCHIVE_PATH),
            entry.options.get(CONF_ARCHIVE_RETENTION_DAYS, DEFAULT_ARCHIVE_RETENTION_DAYS),
        )

        trigger_entities = entry.options.get(CONF_TRIGGER_ENTITIES) or []
        if trigger_entities:
            settle_seconds = entry.options.get(
                CONF_TRIGGER_SETTLE_SECONDS, DEFAULT_TRIGGER_SETTLE_SECONDS
            )
            _register_recording_triggers(
                hass, entry, _start_recent_sync, trigger_entities, settle_seconds
            )
            _LOGGER.info(
                "Recording sync for %s will also trigger %ds after any of %s clears",
                entry.title,
                settle_seconds,
                ", ".join(trigger_entities),
            )
    elif entry.options.get(CONF_TRIGGER_ENTITIES):
        _LOGGER.warning(
            "Trigger entities are configured for %s but no archive folder is set, "
            "so nothing will be downloaded; set one under Configure, or clear the "
            "trigger entities",
            entry.title,
        )
    else:
        # Never stay silent about this: an entry with no archive folder simply
        # downloads nothing, and without a line here the only symptom is an
        # archive that never fills up, with no clue why.
        _LOGGER.info(
            "No archive folder set for %s, so recordings are not being downloaded; "
            "only the recording-schedule switches are active. Set a folder under "
            "Settings > Devices & Services > Reolink Manager > Configure.",
            entry.title,
        )

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ReolinkManagerConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _LOGGER.info("Unloaded Reolink Manager entry %s", entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
