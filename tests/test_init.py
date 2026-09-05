"""Tests for the detection-triggered recording sync in __init__.py."""

from unittest.mock import MagicMock

import custom_components.reolink_manager as reolink_manager
from custom_components.reolink_manager import _is_off_transition, _register_recording_triggers


def _state(value: str | None) -> MagicMock | None:
    if value is None:
        return None
    return MagicMock(state=value)


def test_on_to_off_is_a_trigger() -> None:
    assert _is_off_transition(_state("on"), _state("off")) is True


def test_off_to_off_is_not_a_trigger() -> None:
    assert _is_off_transition(_state("off"), _state("off")) is False


def test_unknown_to_off_is_not_a_trigger() -> None:
    """A sensor settling to 'off' from unknown/unavailable at startup has not just finished detecting."""
    assert _is_off_transition(_state("unknown"), _state("off")) is False
    assert _is_off_transition(_state("unavailable"), _state("off")) is False


def test_on_to_on_is_not_a_trigger() -> None:
    assert _is_off_transition(_state("on"), _state("on")) is False


def test_missing_old_or_new_state_is_not_a_trigger() -> None:
    assert _is_off_transition(None, _state("off")) is False
    assert _is_off_transition(_state("on"), None) is False


# --- debounce -----------------------------------------------------------


class _FakeCancel:
    """Stands in for the callback async_call_later/async_track_state_change_event return."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> None:
        self.called = True


def _patch_event_helpers(monkeypatch):
    """Capture the state-change handler and every scheduled callback+delay."""
    captured: dict = {"handler": None, "scheduled": []}

    def _fake_track(_hass, _entities, handler):
        captured["handler"] = handler
        return _FakeCancel()

    def _fake_call_later(_hass, delay, callback_fn):
        cancel = _FakeCancel()
        captured["scheduled"].append({"delay": delay, "callback": callback_fn, "cancel": cancel})
        return cancel

    monkeypatch.setattr(reolink_manager, "async_track_state_change_event", _fake_track)
    monkeypatch.setattr(reolink_manager, "async_call_later", _fake_call_later)
    return captured


def _change_event(old: str | None, new: str | None, entity_id: str = "binary_sensor.animal") -> MagicMock:
    event = MagicMock()
    event.data = {
        "entity_id": entity_id,
        "old_state": _state(old),
        "new_state": _state(new),
    }
    return event


def test_off_transition_schedules_one_sync(monkeypatch) -> None:
    captured = _patch_event_helpers(monkeypatch)
    hass = MagicMock()
    entry = MagicMock()
    entry.async_on_unload = MagicMock()
    entry_data = {"start_sync": MagicMock()}

    _register_recording_triggers(hass, entry, entry_data, ["binary_sensor.animal"], 60)
    captured["handler"](_change_event("on", "off"))

    assert len(captured["scheduled"]) == 1
    assert captured["scheduled"][0]["delay"] == 60

    captured["scheduled"][0]["callback"](None)
    entry_data["start_sync"].assert_called_once()


def test_second_clear_before_settle_cancels_and_reschedules(monkeypatch) -> None:
    """A sensor flapping during the settle window should push the sync later, not fire twice."""
    captured = _patch_event_helpers(monkeypatch)
    hass = MagicMock()
    entry = MagicMock()
    entry.async_on_unload = MagicMock()
    entry_data = {"start_sync": MagicMock()}

    _register_recording_triggers(hass, entry, entry_data, ["binary_sensor.animal"], 60)
    captured["handler"](_change_event("on", "off"))
    first_cancel = captured["scheduled"][0]["cancel"]

    captured["handler"](_change_event("on", "off", "binary_sensor.person"))

    assert first_cancel.called is True
    assert len(captured["scheduled"]) == 2

    # Only the second (still pending) timer firing should trigger a sync.
    captured["scheduled"][1]["callback"](None)
    entry_data["start_sync"].assert_called_once()


def test_non_off_transition_does_not_schedule(monkeypatch) -> None:
    captured = _patch_event_helpers(monkeypatch)
    hass = MagicMock()
    entry = MagicMock()
    entry.async_on_unload = MagicMock()
    entry_data = {"start_sync": MagicMock()}

    _register_recording_triggers(hass, entry, entry_data, ["binary_sensor.animal"], 60)
    captured["handler"](_change_event("off", "off"))

    assert captured["scheduled"] == []
    entry_data["start_sync"].assert_not_called()
