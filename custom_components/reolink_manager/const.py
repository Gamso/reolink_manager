"""Constants for Reolink Manager."""
from __future__ import annotations

DOMAIN = "reolink_manager"

CONF_REOLINK_ENTRY_ID = "reolink_entry_id"

# Recording-schedule trigger keys as returned by the camera's GetRecV20/SetRecV20
# "schedule.table" (see the Reolink HTTP API guide). reolink_aio caches the raw
# table but never parses or exposes it; only the keys actually present for a
# given camera/firmware get an entity, so this maps a key to the entity's
# translation_key (names in translations/*.json), not an allow-list. Anything
# found that isn't listed here still gets an entity, translated through
# TRIGGER_FALLBACK_TRANSLATION_KEY with the raw key as a placeholder.
TRIGGER_TRANSLATION_KEYS: dict[str, str] = {
    "MD": "motion_recording",
    "TIMING": "continuous_recording",
    "AI_PEOPLE": "person_recording",
    "AI_VEHICLE": "vehicle_recording",
    "AI_ANIMAL": "animal_recording",
    "AI_DOG_CAT": "pet_recording",
    "AI_FACE": "face_recording",
}
TRIGGER_FALLBACK_TRANSLATION_KEY = "trigger_recording"

# --- Recording archive (options flow) ---------------------------------------
# No separate on/off flag: the archive runs when CONF_ARCHIVE_PATH is set, and
# is off when it is left empty.
CONF_ARCHIVE_PATH = "archive_path"
CONF_ARCHIVE_RETENTION_DAYS = "archive_retention_days"
CONF_ARCHIVE_INTERVAL_HOURS = "archive_interval_hours"
CONF_ARCHIVE_STREAM = "archive_stream"

DEFAULT_ARCHIVE_RETENTION_DAYS = 7
DEFAULT_ARCHIVE_INTERVAL_HOURS = 6
DEFAULT_ARCHIVE_STREAM = "main"
ARCHIVE_STREAMS = ["main", "sub"]

# Written at the root of the archive directory the first time it is used -
# but only when that directory was just created or is empty; a non-empty
# directory without it is refused (options flow and every pass). Pruning
# refuses to run when it is missing, so pointing the archive at a
# pre-existing directory (an external disk's root, say) can never delete
# anything this integration did not put there. The archive root itself is
# the only directory ever created: its parent must exist, so an unmounted
# disk is never recreated on the mount point.
ARCHIVE_MARKER_FILENAME = ".reolink_manager_archive"

# The first sync is deferred this long after startup so a catch-up download of
# several days of footage doesn't compete with Home Assistant booting - and,
# more importantly, doesn't hammer the camera while the official integration is
# still setting up its own streams on the same connection.
ARCHIVE_INITIAL_DELAY_SECONDS = 300

# --- Detection-triggered sync (options flow) --------------------------------
# Watches binary_sensor entities (typically the reolink integration's own
# person/animal/vehicle sensors) and syncs shortly after one clears, on top of
# the periodic pass, so a new recording shows up in the archive without
# waiting for the next scheduled run.
CONF_TRIGGER_ENTITIES = "trigger_entities"
CONF_TRIGGER_SETTLE_SECONDS = "trigger_settle_seconds"

# The camera needs a moment after a detection ends to close and finalize the
# recording file before it is listed by GetVodFile; syncing immediately on the
# 1->0 transition would frequently miss the file entirely.
DEFAULT_TRIGGER_SETTLE_SECONDS = 60

SERVICE_SYNC_RECORDINGS = "sync_recordings"


# Dispatched (formatted with the entry id) when the official Reolink entry stops
# being usable, so the switches re-evaluate their availability.
SIGNAL_REOLINK_AVAILABILITY = DOMAIN + "_reolink_availability_{}"
