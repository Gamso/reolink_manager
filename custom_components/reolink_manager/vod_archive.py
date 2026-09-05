"""Download Reolink recordings to a local archive and prune old copies.

Playing recordings straight from the camera is slow (they stream over the
camera's own HTTP interface, which is not built for seeking through days of
footage). This module keeps a local mirror instead: a periodic pass lists the
recordings the camera holds for the retention window, downloads the ones not
already on disk, and deletes archived copies that have aged out.

Everything goes through the `reolink_aio` `Host` the official integration
already owns (see __init__.py), so the archive shares the camera's single
connection rather than opening a competing one. Downloads therefore run
strictly one at a time.

Pruning only ever touches this integration's own archive tree, and only when
the marker file it writes at the root is present - see `_prune_archive`. It
never deletes anything from the camera itself.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify

from .const import ARCHIVE_MARKER_FILENAME

_LOGGER = logging.getLogger(__name__)

DATE_DIR_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
BYTES_PER_GIB = 1024**3

# A recording whose end time is this recent may still be being written by the
# camera. Downloading it now would store a truncated file under its final
# name, and the existence check would never re-fetch it; leave it for the next
# pass instead.
STILL_RECORDING_MARGIN = timedelta(minutes=2)

# Downloads land on `<name>.mp4.part` and are renamed only once complete, so an
# interrupted run never leaves a short file that looks like a finished one.
PART_SUFFIX = ".part"

MARKER_CONTENT = (
    "This directory is a Reolink Manager recording archive.\n"
    "Files under <camera>/<YYYY-MM-DD>/ are managed (and pruned after the\n"
    "configured retention window) by the reolink_manager Home Assistant\n"
    "integration. Deleting this marker file disables pruning.\n"
)


def _prepare_root(root: Path) -> None:
    """Create the archive root and its marker file. Blocking; run in an executor."""
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ARCHIVE_MARKER_FILENAME
    if not marker.exists():
        marker.write_text(MARKER_CONTENT, encoding="utf-8")


def _prune_archive(root: Path, cutoff: date) -> int:
    """Delete archived recordings from before *cutoff*. Blocking; run in an executor.

    Deliberately narrow, because the archive root is a user-supplied path that
    may well be an external disk holding other data:

    * refuses to do anything unless the marker file written by `_prepare_root`
      is present, so it can only ever run against a tree this integration made;
    * only descends into `<camera>/<YYYY-MM-DD>/` directories, and only ones
      whose name really parses as a date;
    * only unlinks `.mp4` files and interrupted `.part` downloads, leaving any
      other file untouched;
    * removes an emptied date directory but gives up silently when it still
      holds something, rather than forcing it.

    Ages are taken from the date directory (the recording's own start date),
    not file mtimes, which would reflect download time instead.
    """
    if not (root / ARCHIVE_MARKER_FILENAME).exists():
        _LOGGER.warning(
            "Not pruning %s: marker file %s is missing, so this directory is not a "
            "Reolink Manager archive (or the marker was removed)",
            root,
            ARCHIVE_MARKER_FILENAME,
        )
        return 0

    removed = 0
    for camera_dir in root.iterdir():
        if not camera_dir.is_dir():
            continue
        for date_dir in camera_dir.iterdir():
            if not date_dir.is_dir() or not DATE_DIR_PATTERN.fullmatch(date_dir.name):
                continue
            try:
                dir_date = date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if dir_date >= cutoff:
                continue

            for entry in date_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix != ".mp4" and not entry.name.endswith(PART_SUFFIX):
                    continue
                entry.unlink()
                removed += 1

            try:
                date_dir.rmdir()
            except OSError:
                # Still holds files we don't manage; leave the directory alone.
                pass

    return removed


def _trigger_slug(vod_file: Any) -> str:
    """Return a filename-safe description of what triggered a recording.

    Iterating a `Flag` composite yields its members in the order they're
    *defined* in the enum, not alphabetically - for reolink_aio's
    `VOD_trigger` that happens to be the same order Reolink's own recording
    browser lists them in (e.g. "Motion Animal Person"). Keeping that order
    (previously this sorted alphabetically) and title-casing each name makes
    an archived filename read the same way as the entry it came from.

    Reads the members off the instance rather than importing the enum, which
    keeps this testable without reolink_aio installed.
    """
    try:
        names = [flag.name.title() for flag in vod_file.triggers if flag.name]
    except TypeError:
        return "Unknown"
    return "_".join(names) or "Unknown"


def _target_path(root: Path, camera_dir: str, vod_file: Any) -> Path:
    """Return where a recording is archived.

    The name is derived from the recording's own start/end time and triggers
    rather than the camera's filename, which varies by model and is sometimes
    just a bare timestamp with no extension. Deriving it means the same
    recording always maps to the same path, which is what makes "already
    downloaded?" a plain existence check.
    """
    start = vod_file.start_time
    name = f"{start:%H%M%S}-{vod_file.end_time:%H%M%S}_{_trigger_slug(vod_file)}.mp4"
    return root / camera_dir / f"{start:%Y-%m-%d}" / name


def _is_still_recording(end_time: datetime, now: datetime) -> bool:
    """Return True when a recording looks like the camera is still writing it."""
    if end_time.tzinfo is None:
        # The camera reported no timezone; compare in naive local terms, which
        # is what its clock is set to in practice.
        now = now.replace(tzinfo=None)
    return end_time > now - STILL_RECORDING_MARGIN


def _vod_size(vod_file: Any) -> int:
    """Return a recording's size in bytes, or 0 when the camera didn't report one.

    `VOD_file.size` reads a key that isn't present on every model/firmware and
    raises if it's missing. It is only used for a log line, so a missing size
    must not take the whole pass down with it.
    """
    try:
        return int(vod_file.size)
    except (KeyError, TypeError, ValueError):
        return 0


@dataclass
class _SyncStats:
    """What one archive pass (or one channel of it) actually did."""

    downloaded: int = 0
    failed: int = 0
    already_archived: int = 0

    def add(self, other: _SyncStats) -> None:
        self.downloaded += other.downloaded
        self.failed += other.failed
        self.already_archived += other.already_archived


class VodArchiver:
    """Mirrors a camera's recordings into a local directory."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Any,
        *,
        root: Path,
        retention_days: int,
        stream: str,
    ) -> None:
        self._hass = hass
        self._api = api
        self._root = root
        self._retention_days = retention_days
        self._stream = stream
        self._running = False

    async def async_sync(self) -> None:
        """Run one archive pass, unless one is already in progress.

        Overlapping passes are skipped rather than queued: they would fight
        over the camera's single connection and re-download the same files.
        """
        if self._running:
            _LOGGER.debug("Archive pass already in progress for %s; skipping", self._root)
            return
        self._running = True
        try:
            await self._async_sync()
        finally:
            self._running = False

    async def _async_sync(self) -> None:
        try:
            await self._hass.async_add_executor_job(_prepare_root, self._root)
        except OSError as err:
            # Most commonly a permissions or read-only mount problem on
            # whatever the archive folder points at (an external disk, say) -
            # worth a clear one-line error instead of a bare stack trace,
            # since this fires on every scheduled/triggered pass until fixed.
            _LOGGER.error(
                "Cannot use %s as the recording archive: %s. Check that Home "
                "Assistant has write access to this path (and that the disk, "
                "if external, is actually mounted read-write).",
                self._root,
                err,
            )
            return

        now = dt_util.now()
        window_start = now - timedelta(days=self._retention_days)
        started = monotonic()
        _LOGGER.debug(
            "Archive pass starting: '%s' stream, recordings since %s, into %s",
            self._stream,
            window_start,
            self._root,
        )

        stats = _SyncStats()
        for channel in self._api.stream_channels:
            stats.add(await self._async_sync_channel(channel, window_start, now))

        cutoff = (now - timedelta(days=self._retention_days)).date()
        removed = await self._hass.async_add_executor_job(_prune_archive, self._root, cutoff)

        # An uneventful pass (nothing new, nothing pruned) is the normal case
        # every few hours, so it stays at debug; anything that actually changed
        # the archive - or failed to - is worth seeing without turning debug on.
        summary = (
            "Archive pass finished in %.0fs: %d downloaded, %d failed, "
            "%d already archived, %d pruned (older than %s)"
        )
        args = (
            monotonic() - started,
            stats.downloaded,
            stats.failed,
            stats.already_archived,
            removed,
            cutoff,
        )
        if stats.downloaded or stats.failed or removed:
            _LOGGER.info(summary, *args)
        else:
            _LOGGER.debug(summary, *args)

    async def _async_list_vod_files(
        self, channel: int, start: datetime, end: datetime, camera_name: str
    ) -> list[Any]:
        """List every recording in [start, end], one calendar day at a time.

        The camera's Search command silently caps how many entries it returns
        per call - there's no truncation flag to detect, it just drops the
        rest. A single request spanning the whole retention window returns
        only a handful of files on a busy camera, most of the day quietly
        missing. Splitting into one request per calendar day (matching what
        Reolink's own recording browser does) keeps each request's result
        count low enough that the cap is never hit in practice.
        """
        all_files: list[Any] = []
        day_start = start
        while day_start.date() <= end.date():
            day_end = min(end, datetime.combine(day_start.date(), time.max, tzinfo=day_start.tzinfo))
            try:
                _, day_files = await self._api.request_vod_files(
                    channel, day_start, day_end, stream=self._stream
                )
            except Exception:  # pylint: disable=broad-exception-caught
                _LOGGER.exception(
                    "Could not list recordings for %s (channel %s) on %s",
                    camera_name,
                    channel,
                    day_start.date(),
                )
            else:
                all_files.extend(day_files)
            day_start = datetime.combine(
                day_start.date() + timedelta(days=1), time.min, tzinfo=day_start.tzinfo
            )
        return all_files

    async def _async_sync_channel(
        self, channel: int, start: datetime, now: datetime
    ) -> _SyncStats:
        """Download every not-yet-archived recording for one channel."""
        camera_name = self._api.camera_name(channel)
        camera_dir = slugify(camera_name) or f"channel_{channel}"
        stats = _SyncStats()

        vod_files = await self._async_list_vod_files(channel, start, now, camera_name)

        # Keyed by target path, because a recording that spans midnight is
        # returned by both days' searches (the camera's Search matches any
        # recording overlapping the window, not only ones starting inside it)
        # and would otherwise be downloaded twice.
        pending: dict[Path, Any] = {}
        seen: set[Path] = set()
        for vod_file in vod_files:
            if _is_still_recording(vod_file.end_time, now):
                continue
            target = _target_path(self._root, camera_dir, vod_file)
            if target in seen:
                continue
            seen.add(target)
            if await self._hass.async_add_executor_job(target.exists):
                stats.already_archived += 1
                continue
            pending[target] = vod_file

        _LOGGER.debug(
            "%s (channel %s): %d recording(s) listed, %d already archived, %d to download",
            camera_name,
            channel,
            len(vod_files),
            stats.already_archived,
            len(pending),
        )

        if not pending:
            return stats

        # Oldest first, so an interrupted run always makes deterministic
        # progress from the far end of the retention window.
        ordered = sorted(pending.items(), key=lambda item: item[1].start_time)
        total_bytes = sum(_vod_size(vod_file) for _, vod_file in ordered)
        _LOGGER.info(
            "Archiving %d recording(s) (%.2f GiB) for %s (channel %s)",
            len(ordered),
            total_bytes / BYTES_PER_GIB,
            camera_name,
            channel,
        )

        for target, vod_file in ordered:
            if await self._async_download(channel, vod_file, target):
                stats.downloaded += 1
            else:
                stats.failed += 1
        return stats

    async def _async_download(self, channel: int, vod_file: Any, target: Path) -> bool:
        """Download one recording to *target*, via a .part file.

        Returns True when the file landed complete. A failure is logged and
        swallowed rather than raised: one unreadable recording must not stop
        the rest of the pass, and it will be retried on the next one.
        """
        part = target.with_name(target.name + PART_SUFFIX)
        await self._hass.async_add_executor_job(_make_parent, target)

        try:
            download = await self._api.download_vod(
                vod_file.file_name,
                start_time=vod_file.start_time_id,
                end_time=vod_file.end_time_id,
                channel=channel,
                stream=self._stream,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.exception("Could not start download of %s", vod_file.file_name)
            return False

        try:
            written = await self._async_write(download, part)
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.exception("Download of %s failed", vod_file.file_name)
            await self._hass.async_add_executor_job(_unlink_quietly, part)
            return False
        finally:
            download.close()

        if download.length and written != download.length:
            _LOGGER.warning(
                "Discarding truncated download of %s: got %d bytes, expected %d",
                vod_file.file_name,
                written,
                download.length,
            )
            await self._hass.async_add_executor_job(_unlink_quietly, part)
            return False

        await self._hass.async_add_executor_job(part.replace, target)
        _LOGGER.debug("Archived %s (%d bytes)", target, written)
        return True

    async def _async_write(self, download: Any, part: Path) -> int:
        """Stream a download to disk, returning the number of bytes written."""
        handle = await self._hass.async_add_executor_job(_open_for_write, part)
        written = 0
        try:
            async for chunk in download.stream.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                await self._hass.async_add_executor_job(handle.write, chunk)
                written += len(chunk)
        finally:
            await self._hass.async_add_executor_job(handle.close)
        return written


def _make_parent(target: Path) -> None:
    """Blocking; run in an executor."""
    target.parent.mkdir(parents=True, exist_ok=True)


def _open_for_write(path: Path):
    """Blocking; run in an executor."""
    return path.open("wb")


def _unlink_quietly(path: Path) -> None:
    """Blocking; run in an executor."""
    path.unlink(missing_ok=True)
