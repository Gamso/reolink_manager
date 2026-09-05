"""Tests for the recording-archive download and prune logic."""

from datetime import date, datetime, timedelta, timezone
from enum import IntFlag, auto
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from custom_components.reolink_manager.const import ARCHIVE_MARKER_FILENAME
from custom_components.reolink_manager.vod_archive import (
    VodArchiver,
    _is_still_recording,
    _prepare_root,
    _prune_archive,
    _target_path,
    _trigger_slug,
    _vod_size,
)


class _Trigger(IntFlag):
    """Stand-in for reolink_aio's VOD_trigger, which behaves the same way."""

    NONE = 0
    MOTION = auto()
    PERSON = auto()
    ANIMAL = auto()


def _vod_file(
    start: datetime,
    end: datetime,
    *,
    triggers: _Trigger = _Trigger.MOTION,
    name: str = "Mp4Record/2026-09-01/RecM01_x.mp4",
    size: int = 1024,
) -> MagicMock:
    vod = MagicMock()
    vod.start_time = start
    vod.end_time = end
    vod.triggers = triggers
    vod.file_name = name
    vod.size = size
    vod.start_time_id = start.strftime("%Y%m%d%H%M%S")
    vod.end_time_id = end.strftime("%Y%m%d%H%M%S")
    return vod


# --- naming -----------------------------------------------------------------


def test_trigger_slug_uses_definition_order_not_alphabetical() -> None:
    """Matches Reolink's own recording browser, which lists triggers in this
    same (definition) order rather than alphabetically."""
    vod = _vod_file(datetime(2026, 9, 1), datetime(2026, 9, 1), triggers=_Trigger.PERSON | _Trigger.MOTION)
    assert _trigger_slug(vod) == "Motion_Person"


def test_trigger_slug_without_triggers_is_unknown() -> None:
    vod = _vod_file(datetime(2026, 9, 1), datetime(2026, 9, 1), triggers=_Trigger.NONE)
    assert _trigger_slug(vod) == "Unknown"


def test_target_path_derives_stable_name_from_recording_times() -> None:
    vod = _vod_file(
        datetime(2026, 9, 1, 14, 30, 5),
        datetime(2026, 9, 1, 14, 31, 0),
        triggers=_Trigger.ANIMAL,
    )
    target = _target_path(Path("/archive"), "front-door", vod)
    assert target == Path("/archive/front-door/2026-09-01/143005-143100_Animal.mp4")


# --- still-recording guard --------------------------------------------------


def test_recording_that_just_ended_is_skipped() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _is_still_recording(now - timedelta(seconds=30), now) is True


def test_finished_recording_is_not_skipped() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _is_still_recording(now - timedelta(minutes=10), now) is False


def test_naive_camera_timestamps_do_not_raise() -> None:
    """Cameras that report no timezone must not break the comparison."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _is_still_recording(datetime(2026, 9, 1, 11, 0), now) is False


# --- pruning ----------------------------------------------------------------


def _seed_archive(root: Path, camera: str, day: str, filenames: list[str]) -> Path:
    day_dir = root / camera / day
    day_dir.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        (day_dir / filename).write_bytes(b"x")
    return day_dir


def test_prune_removes_old_recordings_and_empty_day(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    old = _seed_archive(tmp_path, "front", "2026-08-01", ["a.mp4", "b.mp4"])

    removed = _prune_archive(tmp_path, date(2026, 9, 1))

    assert removed == 2
    assert not old.exists()


def test_prune_keeps_recordings_inside_retention(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    kept = _seed_archive(tmp_path, "front", "2026-09-05", ["a.mp4"])

    assert _prune_archive(tmp_path, date(2026, 9, 1)) == 0
    assert (kept / "a.mp4").exists()


def test_prune_removes_interrupted_part_downloads(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    old = _seed_archive(tmp_path, "front", "2026-08-01", ["a.mp4.part"])

    assert _prune_archive(tmp_path, date(2026, 9, 1)) == 1
    assert not old.exists()


def test_prune_refuses_without_marker_file(tmp_path: Path) -> None:
    """Pointing the archive at a pre-existing directory must never delete anything."""
    old = _seed_archive(tmp_path, "front", "2026-08-01", ["a.mp4"])

    assert _prune_archive(tmp_path, date(2026, 9, 1)) == 0
    assert (old / "a.mp4").exists()


def test_prune_leaves_foreign_files_and_their_directory(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    old = _seed_archive(tmp_path, "front", "2026-08-01", ["a.mp4", "notes.txt"])

    assert _prune_archive(tmp_path, date(2026, 9, 1)) == 1
    assert not (old / "a.mp4").exists()
    assert (old / "notes.txt").exists()
    assert old.exists()


def test_prune_ignores_non_date_directories(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    other = tmp_path / "front" / "exports"
    other.mkdir(parents=True)
    (other / "keep.mp4").write_bytes(b"x")

    assert _prune_archive(tmp_path, date(2026, 9, 1)) == 0
    assert (other / "keep.mp4").exists()


def test_prepare_root_is_idempotent_and_preserves_marker(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    _prepare_root(root)
    marker = root / ARCHIVE_MARKER_FILENAME
    marker.write_text("custom", encoding="utf-8")

    _prepare_root(root)

    assert marker.read_text(encoding="utf-8") == "custom"


# --- sync -------------------------------------------------------------------


def _hass() -> MagicMock:
    """Home Assistant stub that runs executor jobs inline."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *a: func(*a))
    return hass


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


def _api(vod_files: list, *, channels: list[int] | None = None, payload: bytes = b"data") -> MagicMock:
    """A fake Host.api whose request_vod_files only returns files that actually
    fall within the queried [start, end] window - like the real camera does,
    and unlike a plain fixed return_value would. This matters now that the
    archiver queries one calendar day at a time (see test_vod_archive's
    per-day tests): a naive mock would hand back every file on every day
    queried, masking bugs in the day-splitting logic.
    """
    api = MagicMock()
    api.stream_channels = channels if channels is not None else [0]
    api.camera_name = MagicMock(return_value="Front Door")

    async def _request_vod_files(_channel, start, end, stream=None):  # noqa: ARG001
        matched = [v for v in vod_files if start <= v.start_time <= end]
        return ([], matched)

    api.request_vod_files = AsyncMock(side_effect=_request_vod_files)
    download = MagicMock()
    download.length = len(payload)
    download.stream = _Stream([payload])
    download.close = MagicMock()
    api.download_vod = AsyncMock(return_value=download)
    api._download = download
    return api


async def test_list_vod_files_queries_one_day_at_a_time(tmp_path: Path) -> None:
    """The camera's Search command silently caps results per call; querying
    one calendar day at a time (like Reolink's own recording browser does)
    is what keeps each call's result count under that cap."""
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    start = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    vod_on_day_2 = _vod_file(datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 2, 8, 1, tzinfo=timezone.utc))
    api = _api([vod_on_day_2])
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")

    found = await archiver._async_list_vod_files(0, start, now, "Front Door")

    assert found == [vod_on_day_2]
    assert api.request_vod_files.call_count == 3  # Sep 1, 2, 3


async def test_list_vod_files_survives_one_days_failure(tmp_path: Path) -> None:
    """A failure listing one day must not stop the other days from being checked."""
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    start = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    vod_on_day_3 = _vod_file(datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 3, 8, 1, tzinfo=timezone.utc))
    api = _api([vod_on_day_3])

    real_request = api.request_vod_files

    async def _flaky(channel, start_arg, end_arg, stream=None):
        if start_arg.day == 2:
            raise OSError("camera hiccup")
        return await real_request(channel, start_arg, end_arg, stream=stream)

    api.request_vod_files = AsyncMock(side_effect=_flaky)
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")

    found = await archiver._async_list_vod_files(0, start, now, "Front Door")

    assert found == [vod_on_day_3]


def test_vod_size_falls_back_to_zero_when_camera_reports_none() -> None:
    """Size is only used for a log line; a model that omits it must not take
    the whole pass down."""
    vod = MagicMock()
    type(vod).size = property(lambda _self: (_ for _ in ()).throw(KeyError("size")))
    assert _vod_size(vod) == 0


async def test_recording_spanning_midnight_is_downloaded_once(tmp_path: Path) -> None:
    """The camera's Search returns any recording overlapping the queried window,
    so one spanning midnight comes back from both days - it must not be
    downloaded twice."""
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    across_midnight = _vod_file(
        datetime(2026, 9, 1, 23, 59, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 0, 0, 30, tzinfo=timezone.utc),
    )

    api = _api([])
    # Both the Sep 1 and Sep 2 windows return the same recording.
    api.request_vod_files = AsyncMock(return_value=([], [across_midnight]))
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")

    stats = await archiver._async_sync_channel(0, start, now)

    assert api.download_vod.call_count == 1
    assert stats.downloaded == 1


async def test_sync_channel_counts_already_archived(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(hours=2), now - timedelta(hours=1, minutes=59))
    api = _api([vod])
    target = _target_path(tmp_path, "front_door", vod)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already here")

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    stats = await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    assert stats.already_archived == 1
    assert stats.downloaded == 0


async def test_failed_download_is_counted_not_raised(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(hours=2), now - timedelta(hours=1, minutes=59))
    api = _api([vod])
    api.download_vod = AsyncMock(side_effect=OSError("camera dropped the connection"))

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    stats = await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    assert stats.failed == 1
    assert stats.downloaded == 0


async def test_sync_downloads_new_recording_atomically(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(hours=2), now - timedelta(hours=1, minutes=59))
    api = _api([vod], payload=b"video-bytes")
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")

    await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    target = _target_path(tmp_path, "front_door", vod)
    assert target.read_bytes() == b"video-bytes"
    assert not target.with_name(target.name + ".part").exists()
    api._download.close.assert_called_once()


async def test_sync_skips_already_archived_recording(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(hours=2), now - timedelta(hours=1, minutes=59))
    api = _api([vod])
    target = _target_path(tmp_path, "front_door", vod)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already here")

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    api.download_vod.assert_not_called()
    assert target.read_bytes() == b"already here"


async def test_sync_skips_recording_still_being_written(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(minutes=3), now - timedelta(seconds=10))
    api = _api([vod])

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    api.download_vod.assert_not_called()


async def test_truncated_download_is_discarded(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    vod = _vod_file(now - timedelta(hours=2), now - timedelta(hours=1, minutes=59))
    api = _api([vod], payload=b"short")
    api._download.length = 999  # camera announced more than it sent

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    target = _target_path(tmp_path, "front_door", vod)
    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


async def test_listing_failure_does_not_abort_the_pass(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    api = _api([])
    api.request_vod_files = AsyncMock(side_effect=OSError("camera unreachable"))

    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    await archiver._async_sync_channel(0, now - timedelta(days=7), now)

    api.download_vod.assert_not_called()


async def test_concurrent_sync_is_skipped(tmp_path: Path) -> None:
    api = _api([])
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")
    archiver._running = True

    await archiver.async_sync()

    api.request_vod_files.assert_not_called()


async def test_unwritable_archive_root_logs_and_does_not_raise(tmp_path: Path) -> None:
    """A permission error on the archive root (e.g. a read-only mount) must not
    crash the background sync task - it should be logged and skipped instead."""
    api = _api([])
    archiver = VodArchiver(_hass(), api, root=tmp_path, retention_days=7, stream="main")

    hass = MagicMock()

    async def _raise_on_prepare(func, *args):
        if func.__name__ == "_prepare_root":
            raise PermissionError("Permission denied")
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_raise_on_prepare)
    archiver._hass = hass

    await archiver.async_sync()  # must not raise

    api.request_vod_files.assert_not_called()
