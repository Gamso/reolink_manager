"""Runtime data shared by a Reolink Manager entry and its platforms."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState

from .vod_archive import VodArchiver


@dataclass
class ReolinkManagerData:
    """What one Reolink Manager entry borrowed from its official Reolink entry.

    `host`, `api` and `coordinator` belong to the official integration and are
    only valid while that entry stays loaded with this very host: reloading
    it (options change, reauth, reconnect) stops the old `ReolinkHost` and
    creates a new one. `is_current()` says whether they still are; __init__.py
    reloads this entry when they are not.
    """

    reolink_entry: ConfigEntry
    host: Any
    api: Any
    coordinator: Any
    archiver: VodArchiver | None = None
    start_sync: Callable[..., None] | None = None
    start_recent_sync: Callable[[], None] | None = None
    sync_task: asyncio.Task | None = field(default=None, repr=False)

    def is_current(self) -> bool:
        """Return True while the borrowed host is still the live one."""
        if self.reolink_entry.state is not ConfigEntryState.LOADED:
            return False
        runtime_data = getattr(self.reolink_entry, "runtime_data", None)
        return getattr(runtime_data, "host", None) is self.host

    def cancel_sync(self) -> None:
        """Stop a running archive pass, e.g. because the host went away."""
        if self.sync_task is not None and not self.sync_task.done():
            self.sync_task.cancel()


type ReolinkManagerConfigEntry = ConfigEntry[ReolinkManagerData]
