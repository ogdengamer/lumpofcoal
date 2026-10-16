"""Settings > Backups: the last backup, Back up now, and Download a backup."""

import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import backup
from tests.test_gallery import USERS, add, clients, ids  # noqa: F401  (fixtures)


@pytest.fixture
def data_dir(clients):
    return Path(clients[0].app.state.settings.data_dir)


def page(client):
    return client.get("/settings/backups").text


def test_without_the_service(clients, data_dir):
    abel, _ = clients
    text = page(abel)
    assert "isn't running" in text and "No backups yet" in text
    assert "Back up now" not in text and "Download a backup" in text
    resp = abel.post("/settings/backups/now")
    assert resp.status_code == 422 and not backup.requested(data_dir)


def test_back_up_now_asks_the_service(clients, data_dir):
    abel, _ = clients
    tomorrow = datetime.now().replace(hour=0, minute=30) + timedelta(days=1)
    backup.write_heartbeat(data_dir, "00:30", 30, tomorrow)
    text = page(abel)
    assert "Every night at 12:30 am" in text and "30 days" in text and "Back up now" in text
    resp = abel.post("/settings/backups/now", follow_redirects=False)
    assert resp.headers["location"] == "/settings/backups?done=requested"
    assert backup.requested(data_dir)
    assert "Reload this page in a minute" in page(abel)


def test_shows_the_last_backup(clients, data_dir):
    abel, _ = clients
    at = datetime.now().replace(hour=0, minute=30, second=0).isoformat()
    (data_dir / backup.STATUS_FILE).write_text(json.dumps(
        {"at": at, "ok": True, "file": "gifts-backup-x.tar.gz", "size": 2_500_000}))
    assert "today at 12:30 am" in page(abel) and "2.5 MB" in page(abel)
    (data_dir / backup.STATUS_FILE).write_text(json.dumps(
        {"at": at, "ok": False, "error": "share not mounted",
         "last_ok": {"at": "2026-10-01T00:30:00", "size": 1_000_000}}))
    text = page(abel)
    assert "The last backup failed" in text and "share not mounted" in text
    assert "Last good backup" in text and "Oct 1" in text


def test_download_holds_everything_even_gifts_for_you(clients, ids, tmp_path):
    abel, jules = clients
    add(jules, title="Telescope", recipients=f"person:{ids['Abel']}")
    text = page(abel)
    assert "including ones for you" in text and "data-confirm" in text
    resp = abel.post("/settings/backups/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert 'attachment; filename="gifts-backup-' in resp.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
        tar.extractall(tmp_path / "out", filter="data")
    conn = sqlite3.connect(tmp_path / "out" / "gifts.db")
    assert ("Telescope",) in conn.execute("SELECT title FROM gifts").fetchall()
    conn.close()


def test_download_cleans_up_after_itself(clients, monkeypatch, tmp_path):
    made = []
    real = backup.write_archive

    def spy(data_dir, target):
        made.append(target)
        real(data_dir, target)

    monkeypatch.setattr(backup, "write_archive", spy)
    clients[0].post("/settings/backups/download")
    assert made and not made[0].parent.exists()
