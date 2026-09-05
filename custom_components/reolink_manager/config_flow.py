"""Config flow for Reolink Manager.

Setup asks only which already-configured Reolink integration entry to attach
to: no host, no credentials, no polling interval - this integration piggybacks
entirely on the connection the official `reolink` integration already holds
open (see __init__.py).

The options flow configures the local recording archive (see vod_archive.py),
which is off by default.
"""
from __future__ import annotations

import os
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ARCHIVE_STREAMS,
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
)


def validate_archive_path(raw_path: str) -> str | None:
    """Return an error key for *raw_path*, or None when it is usable.

    An empty path is not an error here: it means the archive is off. Callers
    that require a path (because the archive is being turned on) check for
    emptiness themselves before calling this.

    Blocking (touches the filesystem); call via an executor. Checking at
    configuration time matters here because the archive typically points at an
    external disk, where a typo would otherwise only surface hours later as a
    failed background pass.
    """
    if not raw_path.strip():
        return None

    path = Path(raw_path)
    if not path.is_absolute():
        return "path_not_absolute"

    if path.exists():
        if not path.is_dir():
            return "path_not_directory"
        if not os.access(path, os.W_OK):
            return "path_not_writable"
        return None

    # The archive root itself is created on first use, but its parent has to
    # exist already - otherwise an unmounted disk would silently get a new
    # directory tree on the mount point instead.
    parent = path.parent
    if not parent.is_dir():
        return "path_parent_missing"
    if not os.access(parent, os.W_OK):
        return "path_not_writable"
    return None


class ReolinkManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Reolink Manager."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return ReolinkManagerOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Let the user pick a loaded Reolink config entry to manage."""
        reolink_entries = {
            entry.entry_id: entry.title
            for entry in self.hass.config_entries.async_entries("reolink")
            if entry.state is ConfigEntryState.LOADED
        }
        # Entries already attached to a Reolink Manager instance shouldn't be offered again.
        already_managed = {
            entry.data[CONF_REOLINK_ENTRY_ID] for entry in self._async_current_entries()
        }
        reolink_entries = {
            entry_id: title
            for entry_id, title in reolink_entries.items()
            if entry_id not in already_managed
        }

        if not reolink_entries:
            return self.async_abort(reason="no_reolink_entries")

        if user_input is not None:
            reolink_entry_id = user_input[CONF_REOLINK_ENTRY_ID]
            await self.async_set_unique_id(reolink_entry_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=reolink_entries[reolink_entry_id],
                data={CONF_REOLINK_ENTRY_ID: reolink_entry_id},
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_REOLINK_ENTRY_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=entry_id, label=title)
                            for entry_id, title in reolink_entries.items()
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema)


class ReolinkManagerOptionsFlow(OptionsFlow):
    """Configure the local recording archive."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Manage the archive options.

        There is no separate on/off switch: the archive runs when a folder is
        set, and is off when the folder is left empty. Wanting neither the
        archive nor the trigger entities nor the schedule switches is simply a
        reason to remove the Reolink Manager entry, not to keep it around in a
        half-configured "disabled" state.
        """
        options = self.config_entry.options
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_path = user_input.get(CONF_ARCHIVE_PATH, "").strip()

            error = await self.hass.async_add_executor_job(validate_archive_path, raw_path)
            if error:
                errors[CONF_ARCHIVE_PATH] = error

            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_ARCHIVE_PATH: raw_path,
                        CONF_ARCHIVE_RETENTION_DAYS: int(user_input[CONF_ARCHIVE_RETENTION_DAYS]),
                        CONF_ARCHIVE_INTERVAL_HOURS: int(user_input[CONF_ARCHIVE_INTERVAL_HOURS]),
                        CONF_ARCHIVE_STREAM: user_input[CONF_ARCHIVE_STREAM],
                        CONF_TRIGGER_ENTITIES: user_input.get(CONF_TRIGGER_ENTITIES, []),
                        CONF_TRIGGER_SETTLE_SECONDS: int(
                            user_input[CONF_TRIGGER_SETTLE_SECONDS]
                        ),
                    }
                )

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ARCHIVE_PATH,
                    default=options.get(CONF_ARCHIVE_PATH, ""),
                ): selector.TextSelector(),
                vol.Required(
                    CONF_ARCHIVE_RETENTION_DAYS,
                    default=options.get(
                        CONF_ARCHIVE_RETENTION_DAYS, DEFAULT_ARCHIVE_RETENTION_DAYS
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_ARCHIVE_INTERVAL_HOURS,
                    default=options.get(
                        CONF_ARCHIVE_INTERVAL_HOURS, DEFAULT_ARCHIVE_INTERVAL_HOURS
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_ARCHIVE_STREAM,
                    default=options.get(CONF_ARCHIVE_STREAM, DEFAULT_ARCHIVE_STREAM),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=ARCHIVE_STREAMS,
                        translation_key="archive_stream",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_TRIGGER_ENTITIES,
                    default=options.get(CONF_TRIGGER_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor", multiple=True)
                ),
                vol.Required(
                    CONF_TRIGGER_SETTLE_SECONDS,
                    default=options.get(
                        CONF_TRIGGER_SETTLE_SECONDS, DEFAULT_TRIGGER_SETTLE_SECONDS
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=600, step=5, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema, errors=errors)
