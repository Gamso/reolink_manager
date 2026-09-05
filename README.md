# Reolink Manager

Custom Home Assistant integration that adds two things the official
[`reolink`](https://www.home-assistant.io/integrations/reolink/) integration
doesn't provide:

1. **Per-trigger recording-schedule switches** (motion / person / vehicle /
   animal / ...), where the official integration only has one global
   `switch.<camera>_record`.
2. **A local recording archive** - a background task that downloads the
   camera's recordings to a disk you choose and keeps the last N days, so
   playback is fast instead of streaming slowly from the camera.

Neither opens its own connection to the camera: both reuse the connection the
official integration already holds.

## Why this exists

The official integration's `switch.record` only flips one flag: whether the
camera's recording schedule (whatever it is) runs at all. It never touches
*which* trigger types (motion, AI person, AI vehicle, AI animal, continuous...)
are scheduled to record. That per-trigger schedule is configured on the
camera/NVR itself (or in the Reolink app), not from Home Assistant.

Reolink's HTTP API *does* support reading and writing that schedule
(`GetRecV20`/`SetRecV20`, see `docs/reolink-camera-http-api-user-guide.pdf`),
and the `reolink_aio` library HA already depends on caches the raw schedule
table - it just never parses or exposes it. Reolink Manager reuses that cache
and that connection directly, so you get toggles like *"Animal recording"* or
*"Person recording"* per camera, without opening a second connection to it.

## How it works

Reolink Manager has no config of its own beyond "which Reolink entry to
manage" - picked once in its config flow. At setup it:

1. Looks up the official Reolink config entry the user selected.
2. Reaches into its already-running `ReolinkHost`
   (`entry.runtime_data.host`) and grabs the underlying `reolink_aio`
   `Host.api` object - the *same* live connection the official integration
   uses, not a new one. (Reolink cameras support a limited number of
   simultaneous connections; a second connection from a naive custom
   integration is exactly the kind of thing that causes drops.)
3. For each channel, reads the cached `Rec.schedule.table` bitstring
   (one character per hour, 7 days x 24h) and creates one switch per trigger
   key actually present for that camera/firmware (`MD`, `AI_PEOPLE`,
   `AI_VEHICLE`, `AI_ANIMAL`, `TIMING`, ...).
4. Turning a switch on/off overwrites that trigger's entire weekly bitstring
   with all-1s or all-0s and sends it back via `SetRecV20`/`SetRec` - a
   blanket on/off, not a partial schedule edit.

Switches are attached to the *same* Home Assistant device as the camera's own
Reolink entities (matching the official integration's device-identifier
scheme), so they show up alongside `switch.<camera>_record` rather than under
a separate device.

## Recording archive

Playing recordings straight from the camera is slow: they stream over the
camera's own HTTP interface, which isn't built for seeking through days of
footage. The archive keeps a local mirror instead.

Enable it in **Settings > Devices & Services > Reolink Manager > Configure**:

There is no separate on/off switch: the archive runs when a folder is set below, and is off when it's left empty.

| Option | Meaning |
| -- | -- |
| Archive folder | Absolute path, e.g. `/media/reolink` or an external disk's mount point. Leave empty to disable the archive. Its **parent** must already exist, so an unmounted disk fails loudly instead of silently filling the mount point. |
| Keep downloaded recordings for | Retention in days (default 7). |
| Check for new recordings every | Interval in hours (default 6). |
| Stream to download | `main` (full resolution, large) or `sub` (low resolution, much smaller). |
| Sync immediately when these clear | Optional list of `binary_sensor` entities - typically this camera's own person/animal/vehicle sensors from the Reolink integration. |
| Wait this long after they clear (seconds) | Settle delay before that immediate sync runs (default 60s). |

A pass runs 5 minutes after startup and then on the configured interval; the
`reolink_manager.sync_recordings` service starts one immediately. **Note that
saving the options reloads the entry, which restarts that 5-minute timer** - so
repeatedly tweaking settings can keep postponing the first catch-up. Call the
service if you don't want to wait.

Every pass re-checks the whole retention window, not just what's new since last
time: it lists what the camera holds and downloads whatever isn't on disk yet.
So a freshly configured archive catches up on the last N days on its own.

### Reading the logs

A pass that changed something logs one INFO summary:

```
Archiving 34 recording(s) (1.12 GiB) for E1 Zoom - Salon (channel 0)
Archive pass finished in 96s: 34 downloaded, 0 failed, 12 already archived, 5 pruned (older than 2026-08-29)
```

A pass that found nothing new stays silent at INFO. For the full picture -
per-day listing results, each archived file, and the debounce countdown when a
detection sensor clears - turn on debug:

```yaml
logger:
  logs:
    custom_components.reolink_manager: debug
```

### Syncing right after a detection

The periodic interval alone means a new recording can sit unarchived for up to
that whole interval. To pick it up sooner, pick the detection sensors under
**"Sync immediately when these clear"** - e.g.
`binary_sensor.e1_zoom_bureau_animal_domestique`. This is additional to the
periodic schedule, not a replacement for it.

The trigger fires on the sensor's 1 -> 0 transition (detected -> clear), not on
0 -> 1, because the recording isn't finished - and so isn't listed by the
camera yet - until the detection *ends*. Even then, the camera needs a moment
to close and finalize the file, which is what the settle delay is for: it's
the wait between the sensor clearing and the sync actually starting. If the
delay is too short, the sync simply won't find the file yet and it gets picked
up by the next periodic or triggered sync instead - nothing is lost, but if
it happens often, increase the delay.

All watched entities share a single debounce: if a second one clears while the
first is still waiting out its delay (e.g. "person" and "animal" both fire a
few seconds apart for one event), the wait restarts from the second instead of
firing twice. A sensor that starts up as `unknown`/`unavailable` and settles to
`off` doesn't trigger anything - only settling *from `on`* counts, since
nothing was actually just detected in the other case.

Layout on disk:

```
<archive folder>/
  .reolink_manager_archive          <- marker file, see below
  front_door/
    2026-09-01/
      143005-143100_Motion_Animal_Person.mp4
```

Triggers are listed in the same order Reolink's own recording browser shows
them (`Motion Animal Person`), so an archived filename reads like the entry it
came from.

Names are derived from each recording's own start/end time and triggers, so the
same recording always maps to the same path - that's what makes "already
downloaded?" a plain existence check. Downloads land on a `.part` file and are
renamed only once complete and size-checked, so an interrupted run never leaves
a truncated file that looks finished.

### What pruning will and won't touch

Retention deletes **archived copies only**. Nothing is ever deleted from the
camera. Because the archive folder is a path you supply - quite possibly an
external disk holding other data - pruning is deliberately narrow. It:

* refuses to run at all unless the `.reolink_manager_archive` marker file is
  present at the root, so it can only act on a tree this integration created;
* only descends into `<camera>/<YYYY-MM-DD>/` directories whose name really
  parses as a date;
* only deletes `.mp4` files and interrupted `.part` downloads, leaving every
  other file alone;
* removes an emptied date directory, but gives up silently if it still holds
  anything else.

Ages come from the date directory (the recording's start date), not file
mtimes, which would reflect download time instead.

### First run

The first pass downloads the whole retention window, which on a busy camera at
`main` resolution can be tens of gigabytes over a slow HTTP interface. Downloads
run strictly one at a time (they share the camera's single connection) and
oldest-first, so an interrupted run makes deterministic progress. The planned
count and total size are logged at INFO before it starts - worth watching the
log on the first pass, and worth considering `sub` if you only need a quick
visual record.

## Requirements

- The official **Reolink** integration, already set up and connected to your
  camera or NVR.
- Your camera/firmware must support `GetRecV20`/`SetRecV20` and must report a
  per-trigger schedule table. If it doesn't, or only exposes trigger types you
  don't care about, no switches (or fewer than expected) are created - check
  the `custom_components.reolink_manager` debug log.
- For the archive: enough free space for the retention window, and a path Home
  Assistant can write to (inside a container, the disk has to be mounted into
  it).

## Installation

1. Copy `custom_components/reolink_manager` into your Home Assistant
   `config/custom_components/` directory (or install via HACS as a custom
   repository).
2. Restart Home Assistant.
3. Settings > Devices & Services > Add Integration > **Reolink Manager**, and
   pick the Reolink entry to manage.
4. Optionally **Configure** it to enable the recording archive.

## Multiple cameras

Already supported, no extra setup beyond repeating the steps above: add one
Reolink Manager entry per Reolink integration entry (entries already managed
aren't offered again in the picker), and each entry runs its own switches,
archive, and triggers independently. Two cameras means two Reolink Manager
entries, each pointed at its own Reolink entry and, if you want the archive,
its own folder and its own detection sensors selected in "Sync immediately
when these clear."

An NVR whose channels all live under a single Reolink entry is handled by that
one Reolink Manager entry instead - switches and the archive both iterate over
every channel it exposes.

## Development

See [`.devcontainer/README.md`](.devcontainer/README.md) for the VS Code
devcontainer setup. There is no simulated-camera fixture: manual end-to-end
testing needs a real Reolink camera or NVR reachable from the container.
