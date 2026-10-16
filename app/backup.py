"""Nightly backups: the database and photos in one dated .tar.gz.

    python -m app.backup DEST [--data-dir /data] [--keep-days 30] [--daily-at HH:MM]

Without --daily-at it makes one backup and exits; with it, it runs forever and
makes one each day at that time (container local time, so set TZ). Each run:
- write to DEST/.tmp-<ts>.tar.gz, then rename to gifts-backup-<ts>.tar.gz, so a
  half-written file never looks like a backup;
- delete gifts-backup-*.tar.gz older than --keep-days;
- any error stops that run (and is logged); the next day's run tries again;
- refuse to write unless DEST holds the .lumpofcoal-backups marker, so an
  unmounted share never fills the local disk instead.

The database is copied with SQLite's backup API, not by copying the file: the app
runs in WAL mode, so the file on disk alone can be missing recent changes. The
copy is checked with PRAGMA integrity_check before it goes into the archive.

The app and this service share the data folder and talk through files there:
the service keeps backup-service.json fresh (so Settings > Backups knows it's
running), the app drops a backup-request file for "Back up now", and the
service writes backup-status.json after every run.

Restore: stop the app, extract the archive into the data folder (it holds
gifts.db and photos/), delete any leftover gifts.db-wal / gifts.db-shm there,
and start the app.
"""

import argparse
import json
import sqlite3
import sys
import tarfile
import tempfile
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

PREFIX = "gifts-backup-"
# Written into the data folder after every run, for the app to show (and so the
# Welcome checklist knows backups are set up).
STATUS_FILE = "backup-status.json"
SERVICE_FILE = "backup-service.json"  # the service's heartbeat and schedule
REQUEST_FILE = "backup-request"  # "Back up now", from Settings
HEARTBEAT_SECONDS = 60
TMP_PREFIX = ".tmp-"
# Created once on the NAS share. The fstab entry uses nofail, so if the NAS is down
# at boot the mount point is an empty local folder: no marker, no backup.
MARKER = ".lumpofcoal-backups"


def integrity(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def snapshot_db(source: Path, target: Path) -> None:
    """A consistent copy of a live SQLite database, checked before use."""
    src = sqlite3.connect(source)  # only read; SQLite needs the -shm file writable
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        result = integrity(dst)
        if result != "ok":
            raise RuntimeError(f"database copy failed its integrity check: {result}")
    finally:
        dst.close()
        src.close()


def write_archive(data_dir: Path, target: Path) -> None:
    """gifts.db (a checked snapshot) and photos/ into one .tar.gz."""
    db = data_dir / "gifts.db"
    if not db.is_file():
        raise FileNotFoundError(f"no database at {db}")
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "gifts.db"
        snapshot_db(db, copy)
        with tarfile.open(target, "w:gz") as tar:
            tar.add(copy, arcname="gifts.db")
            photos = data_dir / "photos"
            if photos.is_dir():
                tar.add(photos, arcname="photos",
                        filter=lambda info: None if info.name.endswith(".tmp") else info)


def archive_name(now: datetime) -> str:
    return f"{PREFIX}{now:%Y%m%d_%H%M%S}.tar.gz"


def make_backup(data_dir: Path, dest: Path, now: datetime) -> Path:
    if not (data_dir / "gifts.db").is_file():
        raise FileNotFoundError(f"no database at {data_dir / 'gifts.db'}")
    if not (dest / MARKER).is_file():
        raise FileNotFoundError(f"{dest}/{MARKER} not found: is the share mounted?")
    final = dest / archive_name(now)
    tmp = dest / f"{TMP_PREFIX}{final.name.removeprefix(PREFIX)}"
    try:
        write_archive(data_dir, tmp)
        tmp.rename(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def prune(dest: Path, keep_days: int, now: datetime) -> list[Path]:
    """Delete backups older than keep_days, and temp files left by a crashed run."""
    cutoff = (now - timedelta(days=keep_days)).timestamp()
    stale_tmp = (now - timedelta(days=1)).timestamp()
    gone = []
    for path in dest.iterdir():
        mtime = path.stat().st_mtime
        if ((path.name.startswith(PREFIX) and path.name.endswith(".tar.gz") and mtime < cutoff)
                or (path.name.startswith(TMP_PREFIX) and mtime < stale_tmp)):
            path.unlink()
            gone.append(path)
    return gone


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stamp(at: datetime | None = None) -> str:
    """A timestamp with its UTC offset: the app may not share this container's TZ."""
    return (at or datetime.now()).astimezone().isoformat(timespec="seconds")


def write_status(data_dir: Path, **status) -> None:
    write_json(data_dir / STATUS_FILE, {"at": stamp(), **status})


def read_status(data_dir: Path) -> dict | None:
    return read_json(Path(data_dir) / STATUS_FILE)


def last_good(status: dict | None) -> dict | None:
    """The most recent successful backup recorded in a status."""
    if not status:
        return None
    if status.get("ok"):
        return {key: status.get(key) for key in ("at", "file", "size")}
    return status.get("last_ok")


def run_once(data_dir: Path, dest: Path, keep_days: int, out=None) -> Path:
    out = out or sys.stdout
    now = datetime.now()
    try:
        made = make_backup(data_dir, dest, now)
    except Exception as exc:
        # Keep the last good one, so Settings can say how old the newest backup is.
        write_status(data_dir, ok=False, error=str(exc), last_ok=last_good(read_status(data_dir)))
        raise
    write_status(data_dir, ok=True, file=made.name, size=made.stat().st_size)
    size = made.stat().st_size / 1_000_000
    print(f"{now:%Y-%m-%d %H:%M:%S} backup written: {made.name} ({size:.1f} MB)", file=out)
    for path in prune(dest, keep_days, now):
        print(f"{now:%Y-%m-%d %H:%M:%S} deleted old backup: {path.name}", file=out)
    return made


def write_heartbeat(data_dir: Path, at: str, keep_days: int, when: datetime,
                    busy: bool = False) -> None:
    write_json(data_dir / SERVICE_FILE, {
        "seen": stamp(), "daily_at": at, "keep_days": keep_days, "next": stamp(when),
        "busy": busy})


def request_backup(data_dir: Path) -> None:
    (Path(data_dir) / REQUEST_FILE).touch()


def requested(data_dir: Path) -> bool:
    return (Path(data_dir) / REQUEST_FILE).exists()


def service_state(data_dir: Path, now: datetime | None = None) -> dict | None:
    """The service's heartbeat, or None if it isn't running (or hasn't been lately).

    A backup in progress doesn't beat, so a busy service gets an hour's grace."""
    beat = read_json(Path(data_dir) / SERVICE_FILE)
    try:
        seen = datetime.fromisoformat(beat["seen"]).astimezone()
    except (TypeError, KeyError, ValueError):
        return None
    grace = timedelta(hours=1) if beat.get("busy") else timedelta(seconds=3 * HEARTBEAT_SECONDS)
    return beat if (now or datetime.now()).astimezone() - seen < grace else None


def next_run(now: datetime, at: str) -> datetime:
    hour, minute = (int(part) for part in at.split(":"))
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def run_logged(data_dir: Path, dest: Path, keep_days: int) -> None:
    try:
        run_once(data_dir, dest, keep_days)
    except Exception:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} BACKUP FAILED", file=sys.stderr)
        traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()


def daily(data_dir: Path, dest: Path, keep_days: int, at: str, poll: float = 5) -> None:
    print(f"backups daily at {at}, kept {keep_days} days, into {dest}", flush=True)
    while True:
        when = next_run(datetime.now(), at)
        beat = None
        # Short steps: a clock change or a suspended host can't skip a day, and
        # "Back up now" is picked up within a few seconds.
        while datetime.now() < when:
            if beat is None or time.monotonic() - beat >= HEARTBEAT_SECONDS:
                write_heartbeat(data_dir, at, keep_days, when)
                beat = time.monotonic()
            if requested(data_dir):
                (data_dir / REQUEST_FILE).unlink(missing_ok=True)
                write_heartbeat(data_dir, at, keep_days, when, busy=True)
                run_logged(data_dir, dest, keep_days)
                beat = None
            time.sleep(min(poll, max(0.1, (when - datetime.now()).total_seconds())))
        write_heartbeat(data_dir, at, keep_days, when, busy=True)
        run_logged(data_dir, dest, keep_days)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path, help="folder to write backups into")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--keep-days", type=int, default=30)
    parser.add_argument("--daily-at", metavar="HH:MM",
                        help="keep running and back up every day at this time")
    args = parser.parse_args(argv)
    if args.daily_at:
        next_run(datetime.now(), args.daily_at)  # reject a bad time before waiting a day
        daily(args.data_dir, args.dest, args.keep_days, args.daily_at)
        return 0
    run_once(args.data_dir, args.dest, args.keep_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
