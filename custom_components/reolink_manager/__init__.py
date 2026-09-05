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
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

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
)
from .vod_archive import VodArchiver

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SWITCH]

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SYNC_RECORDINGS_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


def _iter_archiver_entries(hass: HomeAssistant) -> list[tuple[str, dict]]:
    """Return loaded entries that have a recording archive configured."""
    return [
        (entry_id, entry_data)
        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items()
        if entry_data.get("archiver") is not None
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
    entry_data: dict,
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
        entry_data["start_sync"]()

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


def _register_services(hass: HomeAssistant) -> None:
    """Register domain services once per Home Assistant instance."""

    async def sync_recordings(call: ServiceCall) -> None:
        """Start an archive pass now instead of waiting for the next interval."""
        entries = _iter_archiver_entries(hass)
        if not entries:
            raise HomeAssistantError(
                "No Reolink Manager entry has the recording archive enabled"
            )

        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id is not None:
            matches = [data for candidate_id, data in entries if candidate_id == entry_id]
            if not matches:
                raise HomeAssistantError(
                    f"No Reolink Manager entry with the recording archive enabled has id {entry_id}"
                )
            target = matches[0]
        elif len(entries) > 1:
            raise HomeAssistantError(
                "Multiple Reolink Manager entries have the recording archive enabled; "
                f"specify {ATTR_CONFIG_ENTRY_ID} in the service call"
            )
        else:
            target = entries[0][1]

        # A pass can run for a long time on a first catch-up, so it is started
        # in the background rather than making the service call block on it.
        target["start_sync"]()

    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_RECORDINGS, sync_recordings, schema=SYNC_RECORDINGS_SCHEMA
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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

    host = reolink_entry.runtime_data.host
    api = host.api
    archiver = _build_archiver(hass, entry, api)

    entry_data = {
        "host": host,
        "api": api,
        "reolink_entry_id": reolink_entry_id,
        "archiver": archiver,
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    if archiver is not None:
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
            entry.async_create_background_task(
                hass, archiver.async_sync(), f"{DOMAIN} archive sync {entry.entry_id}"
            )

        entry_data["start_sync"] = _start_sync

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
                hass, entry, entry_data, trigger_entities, settle_seconds
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

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_RECORDINGS):
        _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        if not domain_data:
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_RECORDINGS)
            hass.data.pop(DOMAIN, None)
        _LOGGER.info("Unloaded Reolink Manager entry %s", entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
