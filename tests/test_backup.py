"""Nightly backups: a consistent database copy plus photos, rotated after 30 days."""

import os
import tarfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import backup
from app.db import make_engine, make_sessionmaker
from app.models import Person

NOW = datetime(2026, 10, 17, 0, 30, 0)


@pytest.fixture
def data(tmp_path, session):
    """A live data folder: the session's database (WAL mode, still open) and photos."""
    for size in ("full", "thumb"):
        (tmp_path / "photos" / size).mkdir(parents=True)
        (tmp_path / "photos" / size / f"{'a' * 32}.webp").write_bytes(b"photo-" + size.encode())
    (tmp_path / "photos" / "full" / "half-written.tmp").write_bytes(b"x")
    return tmp_path


@pytest.fixture
def dest(tmp_path_factory):
    folder = tmp_path_factory.mktemp("backups")
    (folder / backup.MARKER).touch()
    return folder


def restore(archive, into):
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="data")
    return make_sessionmaker(make_engine(str(into)))()


def test_backup_holds_the_database_and_photos(data, dest, tmp_path_factory):
    made = backup.make_backup(data, dest, NOW)
    assert made.name == "gifts-backup-20261017_003000.tar.gz"
    assert sorted(p.name for p in dest.iterdir()) == [backup.MARKER, made.name]  # the temp file was renamed
    with tarfile.open(made) as tar:
        names = set(tar.getnames())
    assert {"gifts.db", f"photos/full/{'a' * 32}.webp", f"photos/thumb/{'a' * 32}.webp"} <= names
    assert "photos/full/half-written.tmp" not in names
    assert not any(n.endswith(("-wal", "-shm")) for n in names)


def test_changes_still_in_the_wal_are_included(data, dest, session, tmp_path_factory):
    session.add(Person(name="Brand New"))
    session.commit()  # committed, but the app keeps it open: it lives in gifts.db-wal for now
    assert (data / "gifts.db-wal").stat().st_size > 0
    restored = restore(backup.make_backup(data, dest, NOW), tmp_path_factory.mktemp("restore"))
    names = set(restored.scalars(select(Person.name)))
    assert "Brand New" in names and "Emma" in names


def test_restored_photos_match(data, dest, tmp_path_factory):
    into = tmp_path_factory.mktemp("restore")
    restore(backup.make_backup(data, dest, NOW), into)
    assert (into / "photos" / "thumb" / f"{'a' * 32}.webp").read_bytes() == b"photo-thumb"


def test_a_failed_backup_leaves_nothing_behind(data, dest, monkeypatch):
    def broken(source, target):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(backup, "snapshot_db", broken)
    with pytest.raises(RuntimeError):
        backup.make_backup(data, dest, NOW)
    assert [p.name for p in dest.iterdir()] == [backup.MARKER]


def test_a_corrupt_copy_is_refused(data, dest, monkeypatch):
    monkeypatch.setattr(backup, "integrity", lambda conn: "*** page 3: btree is corrupt")
    with pytest.raises(RuntimeError, match="integrity check"):
        backup.make_backup(data, dest, NOW)
    assert [p.name for p in dest.iterdir()] == [backup.MARKER]


@pytest.mark.parametrize("folder", ["missing", "unmounted"])
def test_no_marker_means_no_backup(data, tmp_path, folder):
    target = tmp_path / folder
    if folder == "unmounted":
        target.mkdir()  # an empty mount point: the NAS didn't mount at boot
    with pytest.raises(FileNotFoundError, match="is the share mounted"):
        backup.make_backup(data, target, NOW)
    assert not target.exists() or list(target.iterdir()) == []


def test_old_backups_are_deleted_after_30_days(dest):
    def file(name, age_days):
        path = dest / name
        path.write_bytes(b"")
        stamp = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))

    file("gifts-backup-old.tar.gz", 31)
    file("gifts-backup-recent.tar.gz", 29)
    file(".tmp-crashed.tar.gz", 2)
    file(".tmp-in-progress.tar.gz", 0)
    file("couchdb-backup-old.tar.gz", 90)  # not ours: left alone
    gone = backup.prune(dest, 30, NOW)
    assert sorted(p.name for p in gone) == [".tmp-crashed.tar.gz", "gifts-backup-old.tar.gz"]
    assert sorted(p.name for p in dest.iterdir()) == [
        backup.MARKER, ".tmp-in-progress.tar.gz", "couchdb-backup-old.tar.gz", "gifts-backup-recent.tar.gz"]


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 16, 23, 0), datetime(2026, 10, 17, 0, 30)),
    (datetime(2026, 10, 17, 0, 30), datetime(2026, 10, 18, 0, 30)),  # just ran: tomorrow
    (datetime(2026, 12, 31, 12, 0), datetime(2027, 1, 1, 0, 30)),
])
def test_next_run(now, expected):
    assert backup.next_run(now, "00:30") == expected


def test_bad_time_is_rejected_straight_away(tmp_path):
    with pytest.raises(ValueError):
        backup.main([str(tmp_path), "--daily-at", "half past"])


def test_command_line_run(data, dest, capsys):
    assert backup.main([str(dest), "--data-dir", str(data)]) == 0
    assert "backup written: gifts-backup-" in capsys.readouterr().out
    assert len(list(dest.glob("gifts-backup-*.tar.gz"))) == 1


# --- the service and the app talk through the data folder -----------------------------

class Stop(Exception):
    pass


def test_back_up_now_is_picked_up_by_the_service(data, dest, monkeypatch):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            assert backup.service_state(data)["daily_at"] == "00:30"
            backup.request_backup(data)
        elif backup.read_status(data):
            raise Stop

    monkeypatch.setattr(backup.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        backup.daily(data, dest, 30, "00:30", poll=0)
    assert not backup.requested(data)
    assert backup.read_status(data)["ok"]
    assert len(list(dest.glob(f"{backup.PREFIX}*.tar.gz"))) == 1


def test_service_state_goes_stale(tmp_path):
    assert backup.service_state(tmp_path) is None
    backup.write_heartbeat(tmp_path, "00:30", 30, NOW)
    assert backup.service_state(tmp_path)["keep_days"] == 30
    later = datetime.now() + timedelta(minutes=5)
    assert backup.service_state(tmp_path, later) is None
    backup.write_heartbeat(tmp_path, "00:30", 30, NOW, busy=True)  # a long backup running
    assert backup.service_state(tmp_path, later) is not None
    (tmp_path / backup.SERVICE_FILE).write_text("[]")
    assert backup.service_state(tmp_path) is None


def test_a_failure_remembers_the_last_good_backup(data, dest):
    made = backup.run_once(data, dest, 30)
    (dest / backup.MARKER).unlink()
    for _ in range(2):  # stays remembered across several failures
        with pytest.raises(FileNotFoundError):
            backup.run_once(data, dest, 30)
    status = backup.read_status(data)
    assert status["ok"] is False and "is the share mounted" in status["error"]
    assert status["last_ok"]["file"] == made.name and status["last_ok"]["size"] > 0


def test_times_carry_their_zone(tmp_path, monkeypatch):
    """The app and the backup container can disagree on TZ; the heartbeat still counts."""
    backup.write_heartbeat(tmp_path, "00:30", 30, NOW)
    beat = backup.read_json(tmp_path / backup.SERVICE_FILE)
    assert datetime.fromisoformat(beat["seen"]).tzinfo is not None
    elsewhere = datetime.fromisoformat(beat["seen"]).astimezone(timezone(timedelta(hours=-7)))
    assert backup.service_state(tmp_path, elsewhere + timedelta(seconds=30)) is not None
