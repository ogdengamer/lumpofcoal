"""App users and password sign-in. Made-up gifts and people."""

import io
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import logins, users as users_cli
from app.config import ConfigError, Settings
from app.main import create_app
from app.models import Invite, LoginSession, Person
from app.visibility import GiftRepo
from tests.test_auth import AUD, TEAM, TRUSTED_KEY, make_token
from app.auth import JWT_HEADER, AccessVerifier

USERS = {"abel@example.com": "Abel", "jules@example.com": "Jules"}
PASSWORD = "correct horse"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(auth_mode="password", users=USERS, data_dir=str(tmp_path)))


def db(app):
    return app.state.sessionmaker()


def person(app, name):
    with db(app) as s:
        return s.scalar(select(Person).where(Person.name == name))


def invite_token(app, name):
    with db(app) as s:
        token = logins.create_invite(s, s.scalar(select(Person).where(Person.name == name)), None)
        s.commit()
        return token


def browser(app, https=False):
    return TestClient(app, base_url="https://testserver" if https else "http://testserver",
                      follow_redirects=False)


def sign_up(app, name, password=PASSWORD, https=False):
    client = browser(app, https)
    resp = client.post(f"/invite/{invite_token(app, name)}",
                       data={"password": password, "again": password})
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def abel(app):
    return sign_up(app, "Abel")


def signed_in(client) -> bool:
    return client.get("/", headers={"accept": "text/html"}).status_code == 200


# --- passwords ---------------------------------------------------------------------

def test_password_hashing():
    stored = logins.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert logins.check_password(PASSWORD, stored)
    assert not logins.check_password("wrong horse", stored)
    assert not logins.check_password(PASSWORD, None)
    assert logins.hash_password(PASSWORD) != stored  # salted


# --- signed out ----------------------------------------------------------------------

def test_signed_out_pages_go_to_sign_in(app):
    client = browser(app)
    resp = client.get("/budget?occasion=3", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fbudget%3Foccasion%3D3"
    assert client.get("/", headers={"accept": "text/html"}).headers["location"] == "/login"
    assert client.post("/gifts", data={}).status_code == 401
    assert client.get("/photos/" + "a" * 32 + "/thumb").status_code == 401


def test_sign_in_page_is_styled_and_shows_no_nav(app):
    client = browser(app)
    page = client.get("/login").text
    css = re.search(r'href="(/static/app\.css[^"]*)"', page).group(1)
    assert client.get(css).status_code == 200
    assert 'href="/budget"' not in page and 'class="who"' not in page


# --- invites ----------------------------------------------------------------------------

def test_invite_signs_you_in_once(app):
    token = invite_token(app, "Jules")
    client = browser(app)
    assert "Hi Jules" in client.get(f"/invite/{token}").text
    resp = client.post(f"/invite/{token}", data={"password": PASSWORD, "again": PASSWORD})
    assert resp.status_code == 303 and signed_in(client)
    again = browser(app).get(f"/invite/{token}")
    assert again.status_code == 404 and "doesn't work any more" in again.text


def test_invite_checks_the_password(app):
    token = invite_token(app, "Jules")
    client = browser(app)
    assert client.post(f"/invite/{token}", data={"password": "short", "again": "short"}).status_code == 422
    assert client.post(f"/invite/{token}", data={"password": PASSWORD, "again": "different!"}).status_code == 422
    assert person(app, "Jules").password_hash is None
    assert client.post(f"/invite/{token}", data={"password": PASSWORD, "again": PASSWORD}).status_code == 303


def test_expired_invite_and_replaced_invite_dont_work(app):
    old = invite_token(app, "Jules")
    new = invite_token(app, "Jules")
    assert browser(app).get(f"/invite/{old}").status_code == 404
    with db(app) as s:
        s.scalar(select(Invite)).expires_at = logins.now() - timedelta(minutes=1)
        s.commit()
    assert browser(app).get(f"/invite/{new}").status_code == 404


def test_only_hashes_are_stored(app, abel):
    token = abel.cookies[logins.COOKIE]
    with db(app) as s:
        stored = [row.token_hash for row in s.scalars(select(LoginSession))]
    assert token not in stored and logins.token_hash(token) in stored


def test_reset_link_signs_out_other_devices(app, abel):
    phone = sign_up(app, "Abel", "another password")  # e.g. a forgotten password
    assert signed_in(phone) and not signed_in(abel)


# --- signing in ---------------------------------------------------------------------------

def test_sign_in_and_out(app, abel):
    client = browser(app, https=True)
    resp = client.post("/login", data={"email": "Abel@Example.com", "password": PASSWORD,
                                       "next": "/budget"})
    assert resp.status_code == 303 and resp.headers["location"] == "/budget"
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "max-age=31536000" in cookie
    assert signed_in(client)
    assert client.post("/logout").headers["location"] == "/login?out=1"
    assert not signed_in(client)


def test_wrong_password_and_unknown_email(app, abel):
    client = browser(app)
    for email, password in [("abel@example.com", "nope nope"), ("who@example.com", PASSWORD)]:
        resp = client.post("/login", data={"email": email, "password": password})
        assert resp.status_code == 401 and "don&#39;t match" in resp.text


def test_no_password_yet_cannot_sign_in(app):
    resp = browser(app).post("/login", data={"email": "jules@example.com", "password": ""})
    assert resp.status_code == 401


@pytest.mark.parametrize("target", ["//evil.example/x", "/\\evil.example", "https://evil.example"])
def test_next_stays_on_this_site(app, abel, target):
    resp = browser(app).post("/login", data={"email": "abel@example.com", "password": PASSWORD,
                                             "next": target})
    assert resp.headers["location"] == "/"


def test_guessing_is_slowed_down(app, abel):
    client = browser(app)
    for _ in range(logins.Throttle.LIMIT):
        client.post("/login", data={"email": "abel@example.com", "password": "guess guess"})
    resp = client.post("/login", data={"email": "abel@example.com", "password": PASSWORD})
    assert resp.status_code == 429  # even the right password, for now


def test_throttle_window():
    clock = [0.0]
    throttle = logins.Throttle(clock=lambda: clock[0])
    for _ in range(5):
        throttle.failed("k")
    assert throttle.blocked("k")
    clock[0] += 15 * 60 + 1
    assert not throttle.blocked("k")


def test_session_lasts_a_year_from_last_use(app, abel):
    with db(app) as s:
        login = s.scalar(select(LoginSession))
        login.last_used_at = logins.now() - timedelta(days=200)
        login.expires_at = logins.now() + timedelta(days=165)
        s.commit()
    assert signed_in(abel)
    with db(app) as s:
        expires = logins.aware(s.scalar(select(LoginSession)).expires_at)
    assert expires > logins.now() + timedelta(days=364)
    with db(app) as s:
        s.scalar(select(LoginSession)).expires_at = logins.now() - timedelta(seconds=1)
        s.commit()
    assert not signed_in(abel)


# --- Settings > App users -----------------------------------------------------------------------

def test_give_a_login_makes_a_link(app, abel):
    emma = person(app, "Emma")
    page = abel.post("/settings/users", data={"person_id": emma.id, "email": "Emma@Example.com"}).text
    link = re.search(r'value="(http://testserver/invite/[^"]+)"', page).group(1)
    assert "Sign-in link for Emma" in page
    client = browser(app)
    token = link.rsplit("/", 1)[1]
    client.post(f"/invite/{token}", data={"password": PASSWORD, "again": PASSWORD})
    assert signed_in(client)
    assert person(app, "Emma").login_email == "emma@example.com"


def test_link_uses_https_behind_a_proxy(app, abel):
    page = abel.post(f"/settings/users/{person(app, 'Jules').id}/invite",
                      headers={"x-forwarded-proto": "https"}).text
    assert 'value="https://testserver/invite/' in page


def test_new_user_still_cant_see_their_gifts(app, abel):
    emma_id = person(app, "Emma").id
    with db(app) as s:
        GiftRepo(s, person(app, "Abel").id).create(title="Made-up Kite", recipient_person_id=emma_id)
        s.commit()
    abel.post("/settings/users", data={"person_id": emma_id, "email": "emma@example.com"})
    emma = sign_up(app, "Emma")
    assert "Made-up Kite" not in emma.get("/").text
    assert "Made-up Kite" in abel.get("/").text


def test_email_must_be_unused(app, abel):
    resp = abel.post("/settings/users", data={"person_id": person(app, "Emma").id,
                                               "email": "jules@example.com"})
    assert "already Jules&#39;s login" in resp.text and person(app, "Emma").login_email is None


def test_remove_login_signs_them_out_at_once(app, abel):
    jules = sign_up(app, "Jules")
    assert signed_in(jules)
    resp = abel.post(f"/settings/users/{person(app, 'Jules').id}/remove")
    assert resp.status_code == 303 and not signed_in(jules)
    gone = person(app, "Jules")
    assert gone.login_email is None and gone.password_hash is None


def test_cant_remove_yourself(app, abel):
    resp = abel.post(f"/settings/users/{person(app, 'Abel').id}/remove")
    assert "your own login" in resp.text and person(app, "Abel").login_email


def test_change_password(app, abel):
    other = browser(app)
    other.post("/login", data={"email": "abel@example.com", "password": PASSWORD})
    me = person(app, "Abel").id
    bad = abel.post("/settings/password", data={"current": "nope", "password": "new password",
                                                 "again": "new password"})
    assert "isn&#39;t right" in bad.text
    abel.post("/settings/password", data={"current": PASSWORD, "password": "new password",
                                           "again": "new password"})
    assert signed_in(abel) and not signed_in(other)
    with db(app) as s:
        assert logins.check_password("new password", s.get(Person, me).password_hash)


def test_users_tab_lists_states(app, abel):
    page = abel.get("/settings/users").text
    assert "abel@example.com" in page and "· you" in page
    assert "needs a sign-in link" in page  # Jules
    assert "Change password" in page and "Sign out" in page


# --- APP_USERS bootstrap -------------------------------------------------------------------------

def test_app_users_only_used_on_a_new_database(tmp_path, session, people):
    assert logins.bootstrap_users(session, {"a@example.com": "Abel"}) == 1
    people["Abel"].login_email = None
    people["Jules"].login_email = "j@example.com"
    session.commit()
    assert logins.bootstrap_users(session, {"a@example.com": "Abel"}) == 0
    assert people["Abel"].login_email is None  # a removed login stays removed


def test_app_users_creates_a_missing_person(session):
    logins.bootstrap_users(session, {"sam@example.com": "Sam"})
    assert session.scalar(select(Person).where(Person.name == "Sam")).login_email == "sam@example.com"


def test_remove_refuses_the_last_login(session, people):
    logins.bootstrap_users(session, {"a@example.com": "Abel"})
    with pytest.raises(Exception, match="last login"):
        logins.remove_login(session, people["Abel"], viewer_id=0)


# --- Cloudflare mode ---------------------------------------------------------------------------

@pytest.fixture
def cf_app(tmp_path):
    settings = Settings(access_team_domain=TEAM, access_aud=AUD, users=USERS, data_dir=str(tmp_path))
    return create_app(settings, AccessVerifier(settings, resolve_key=lambda _t: TRUSTED_KEY.public_key()))


def cf_get(app, email, path="/"):
    return TestClient(app).get(path, headers={JWT_HEADER: make_token(email=email)})


def test_cloudflare_give_and_remove_login(cf_app):
    abel = TestClient(cf_app, headers={JWT_HEADER: make_token()}, follow_redirects=False)
    assert cf_get(cf_app, "emma@example.com").status_code == 403
    page = abel.post("/settings/users", data={"person_id": person(cf_app, "Emma").id,
                                               "email": "emma@example.com"}).text
    assert "Cloudflare Access policy" in page and "/invite/" not in page
    assert cf_get(cf_app, "emma@example.com").status_code == 200
    abel.post(f"/settings/users/{person(cf_app, 'Emma').id}/remove")
    assert cf_get(cf_app, "emma@example.com").status_code == 403


def test_cloudflare_mode_has_no_password_pages(cf_app):
    assert TestClient(cf_app).get("/login").status_code == 401  # not public: behind Access
    page = cf_get(cf_app, "abel@example.com", "/settings/users").text
    assert "Change password" not in page and "Sign-in link" not in page
    assert cf_get(cf_app, "abel@example.com", "/login").status_code == 404


def test_rename_keeps_the_login(cf_app):
    with db(cf_app) as s:
        s.scalar(select(Person).where(Person.name == "Abel")).name = "Abel S."
        s.commit()
    resp = cf_get(cf_app, "abel@example.com")
    assert resp.status_code == 200 and 'class="who">Abel S.<' in resp.text


# --- config ------------------------------------------------------------------------------------

def test_auth_mode_config():
    Settings(auth_mode="password").validate()  # no Cloudflare settings needed
    with pytest.raises(ConfigError):
        Settings(auth_mode="magic").validate()
    with pytest.raises(ConfigError, match="AUTH_MODE=password"):
        Settings().validate()


# --- command line ------------------------------------------------------------------------------

def test_cli_invite_lets_you_in(app, tmp_path):
    out = io.StringIO()
    code = users_cli.main(["--data-dir", str(tmp_path), "invite", "Sam", "--email", "sam@example.com",
                           "--url", "https://gifts.example.com/"], out=out)
    assert code == 0
    token = re.search(r"https://gifts\.example\.com/invite/(\S+)", out.getvalue()).group(1)
    client = browser(app)
    client.post(f"/invite/{token}", data={"password": PASSWORD, "again": PASSWORD})
    assert signed_in(client)
    out = io.StringIO()
    users_cli.main(["--data-dir", str(tmp_path), "list"], out=out)
    assert "Sam: sam@example.com (has a password)" in out.getvalue()


def test_cli_needs_an_email_for_someone_new(tmp_path, app):
    out = io.StringIO()
    assert users_cli.main(["--data-dir", str(tmp_path), "invite", "Nobody"], out=out) == 1
    assert "Add --email" in out.getvalue()
