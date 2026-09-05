"""Tests for the Reolink Manager config flow."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntryState

from custom_components.reolink_manager.config_flow import (
    ReolinkManagerConfigFlow,
    validate_archive_path,
)
from custom_components.reolink_manager.const import CONF_REOLINK_ENTRY_ID


def _reolink_entry(entry_id: str, title: str, state=ConfigEntryState.LOADED) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.title = title
    entry.state = state
    return entry


def _managed_entry(reolink_entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.data = {CONF_REOLINK_ENTRY_ID: reolink_entry_id}
    return entry


def _make_flow(reolink_entries: list, current_entries: list | None = None) -> ReolinkManagerConfigFlow:
    flow = ReolinkManagerConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entries = MagicMock(return_value=reolink_entries)
    flow._async_current_entries = MagicMock(return_value=current_entries or [])
    return flow


async def test_aborts_when_no_reolink_entry_loaded() -> None:
    """No candidate should be offered when Reolink itself has no loaded entry."""
    flow = _make_flow([_reolink_entry("r1", "Front door", state=ConfigEntryState.SETUP_ERROR)])

    result = await flow.async_step_user()

    assert result["type"] == "abort"
    assert result["reason"] == "no_reolink_entries"


async def test_aborts_when_all_reolink_entries_already_managed() -> None:
    """An entry already attached to a Reolink Manager instance is not offered again."""
    flow = _make_flow(
        [_reolink_entry("r1", "Front door")],
        current_entries=[_managed_entry("r1")],
    )

    result = await flow.async_step_user()

    assert result["type"] == "abort"
    assert result["reason"] == "no_reolink_entries"


async def test_shows_form_with_only_unmanaged_loaded_entries() -> None:
    """Only loaded, not-yet-managed Reolink entries should appear as options."""
    flow = _make_flow(
        [
            _reolink_entry("r1", "Front door"),
            _reolink_entry("r2", "Backyard", state=ConfigEntryState.NOT_LOADED),
            _reolink_entry("r3", "Garage"),
        ],
        current_entries=[_managed_entry("r3")],
    )

    result = await flow.async_step_user()

    assert result["type"] == "form"
    schema_keys = list(result["data_schema"].schema.keys())
    assert schema_keys[0] == CONF_REOLINK_ENTRY_ID


async def test_creates_entry_from_selection() -> None:
    """Submitting the form creates an entry pinned to the selected Reolink entry."""
    flow = _make_flow([_reolink_entry("r1", "Front door")])
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    result = await flow.async_step_user({CONF_REOLINK_ENTRY_ID: "r1"})

    assert result["type"] == "create_entry"
    assert result["title"] == "Front door"
    assert result["data"] == {CONF_REOLINK_ENTRY_ID: "r1"}


# --- archive path validation ------------------------------------------------


def test_archive_path_accepts_existing_writable_directory(tmp_path: Path) -> None:
    assert validate_archive_path(str(tmp_path)) is None


def test_archive_path_accepts_missing_dir_with_existing_parent(tmp_path: Path) -> None:
    """The archive root itself is created on first use."""
    assert validate_archive_path(str(tmp_path / "reolink")) is None


def test_archive_path_empty_means_archive_disabled_not_an_error() -> None:
    """An empty path is a valid state (archive off), not a validation error."""
    assert validate_archive_path("   ") is None


def test_archive_path_rejects_relative() -> None:
    assert validate_archive_path("media/reolink") == "path_not_absolute"


def test_archive_path_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    assert validate_archive_path(str(target)) == "path_not_directory"


def test_archive_path_rejects_missing_parent(tmp_path: Path) -> None:
    """An unmounted external disk must fail here, not silently fill the mount point."""
    assert (
        validate_archive_path(str(tmp_path / "unmounted" / "reolink"))
        == "path_parent_missing"
    )
