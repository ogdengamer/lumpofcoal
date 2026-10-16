"""First run on a brand-new copy, and the Welcome checklist. Made-up people."""

import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import app.main
from app import backup, welcome
from app.config import Settings
from app.db import alembic_config, make_engine
from app.models import AppSetting, Person
from app.seed import seed

PASSWORD = "correct horse"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A brand-new copy: the real first-run defaults, not the test household."""
    monkeypatch.setattr(app.main, "seed", seed)
    return create(tmp_path)


def create(tmp_path, **settings):
    return app.main.create_app(Settings(auth_mode="password", data_dir=str(tmp_path), **settings))


def browser(application):
    return TestClient(application, follow_redirects=False)


def html(client, path):
    return client.get(path, headers={"accept": "text/html"})


def set_up(client, name="Sam", email="sam@example.com"):
    return client.post("/setup", data={"name": name, "email": email, "password": PASSWORD,
                                       "again": PASSWORD})


# --- first run ---------------------------------------------------------------------

def test_new_copy_goes_to_setup(fresh):
    client = browser(fresh)
    assert html(client, "/").headers["location"] == "/setup"
    assert html(client, "/settings").headers["location"] == "/setup"
    assert "Welcome to Lump of Coal" in client.get("/setup").text


def test_setup_creates_the_first_login_then_closes(fresh):
    client = browser(fresh)
    resp = set_up(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/welcome"
    assert html(client, "/welcome").status_code == 200
    with fresh.state.sessionmaker() as s:
        sam = s.scalar(select(Person).where(Person.name == "Sam"))
        assert sam.login_email == "sam@example.com" and sam.password_hash
        assert sam.relation.name == "Partner"
    # Nobody else can use it now.
    stranger = browser(fresh)
    assert stranger.get("/setup").headers["location"] == "/login"
    assert set_up(stranger, "Mallory", "m@example.com").headers["location"] == "/login"
    assert html(stranger, "/").headers["location"] == "/login"


def test_setup_checks_its_fields(fresh):
    client = browser(fresh)
    assert client.post("/setup", data={"name": "", "email": "sam@example.com",
                                       "password": PASSWORD, "again": PASSWORD}).status_code == 422
    assert client.post("/setup", data={"name": "Sam", "email": "nope", "password": PASSWORD,
                                       "again": PASSWORD}).status_code == 422
    assert client.post("/setup", data={"name": "Sam", "email": "sam@example.com",
                                       "password": "short", "again": "short"}).status_code == 422
    assert set_up(client).status_code == 303


def test_no_setup_page_in_cloudflare_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(app.main, "seed", seed)
    application = app.main.create_app(Settings(users={"sam@example.com": "Sam"},
                                               dev_user="sam@example.com", data_dir=str(tmp_path)))
    assert browser(application).get("/setup").status_code == 404


# --- the checklist ---------------------------------------------------------------------------

def test_checklist_opens_once_per_launch_until_hidden(fresh):
    client = browser(fresh)
    set_up(client)
    client.cookies.delete(welcome.SEEN_COOKIE)
    assert html(client, "/").headers["location"] == "/welcome"
    html(client, "/welcome")
    assert html(client, "/").status_code == 200  # seen this launch
    assert html(client, "/?status=idea").status_code == 200  # a filtered link never redirects
    client.cookies.delete(welcome.SEEN_COOKIE)  # the app is opened again later
    assert html(client, "/").headers["location"] == "/welcome"
    client.post("/welcome/dismiss")
    client.cookies.delete(welcome.SEEN_COOKIE)
    assert html(client, "/").status_code == 200
    page = html(client, "/welcome").text  # still reachable from Settings
    assert "Getting started" in page and "Hide this checklist" not in page
    assert 'href="/welcome"' in html(client, "/settings/people").text


def test_steps_tick_themselves_off(fresh, tmp_path):
    client = browser(fresh)
    set_up(client)

    def done():
        with fresh.state.sessionmaker() as s:
            return [step.done for step in welcome.steps(s, str(tmp_path))]

    assert done() == [False, False, False, False]
    client.post("/settings/people", data={"name": "Alex", "active": "1"})
    assert done()[1] is False  # Alex needs a relation
    with fresh.state.sessionmaker() as s:
        s.scalar(select(Person).where(Person.name == "Alex")).relation_id = 2
        s.commit()
    assert done()[1] is True
    client.post("/settings/people", data={"name": "Robin", "relation_id": "1", "active": "1"})
    with fresh.state.sessionmaker() as s:
        robin_id = s.scalar(select(Person.id).where(Person.name == "Robin"))
    client.post("/settings/users", data={"person_id": robin_id, "email": "robin@example.com"})
    assert done()[:2] == [True, True]
    client.post("/settings/budgets", data={"occasion_type_id": 1, "target": "relation:2",
                                           "amount": "50"})
    assert done()[2] is True
    (tmp_path / backup.STATUS_FILE).write_text(json.dumps({"ok": True}))
    assert done() == [True, True, True, True]
    assert "4 of 4 done" in html(client, "/welcome").text


def test_backup_writes_its_status(tmp_path, session):
    dest = tmp_path / "nas"
    dest.mkdir()
    with pytest.raises(FileNotFoundError):
        backup.run_once(tmp_path, dest, 30)
    assert backup.read_status(tmp_path)["ok"] is False
    (dest / backup.MARKER).touch()
    backup.run_once(tmp_path, dest, 30)
    status = backup.read_status(tmp_path)
    assert status["ok"] and status["file"].startswith("gifts-backup-")


# --- existing copies ----------------------------------------------------------------------------

def test_existing_household_starts_dismissed(tmp_path):
    """Upgrading a database that already has people (like the original household)."""
    engine = make_engine(str(tmp_path))
    cfg = alembic_config(engine)
    command.upgrade(cfg, "0005")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO people (name, active) VALUES ('Pat', 1)"))
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT value FROM app_settings "
                                 "WHERE key = 'welcome_dismissed'")).scalar() == "1"


def test_test_household_never_sees_the_checklist(session):
    assert session.get(AppSetting, welcome.DISMISSED) is not None
