import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import JWT_HEADER, AccessVerifier
from app.config import ConfigError, Settings, _parse_users
from app.main import create_app

TEAM = "example.cloudflareaccess.com"
AUD = "test-aud-tag"
USERS = {"abel@example.com": "Abel", "jules@example.com": "Jules"}

TRUSTED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(key=TRUSTED_KEY, **overrides):
    now = int(time.time())
    claims = {
        "email": "abel@example.com",
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def client(tmp_path):
    settings = Settings(access_team_domain=TEAM, access_aud=AUD, users=USERS,
                        data_dir=str(tmp_path))
    verifier = AccessVerifier(settings, resolve_key=lambda _token: TRUSTED_KEY.public_key())
    return TestClient(create_app(settings, verifier))


def signed_in_as(resp) -> str | None:
    match = re.search(r'class="who">([^<]+)<', resp.text)
    return match and match.group(1)


def get(client, token=None, **headers):
    if token is not None:
        headers[JWT_HEADER] = token
    return client.get("/", headers=headers)


def test_valid_token_logs_in_as_linked_person(client):
    resp = get(client, make_token())
    assert resp.status_code == 200
    assert signed_in_as(resp) == "Abel"


def test_email_claim_is_case_insensitive(client):
    resp = get(client, make_token(email="Jules@Example.com"))
    assert resp.status_code == 200
    assert signed_in_as(resp) == "Jules"


def test_missing_token_is_rejected(client):
    assert get(client).status_code == 401


def test_spoofed_email_header_alone_is_rejected(client):
    resp = get(client, **{"Cf-Access-Authenticated-User-Email": "abel@example.com"})
    assert resp.status_code == 401


def test_spoofed_email_header_does_not_override_token(client):
    resp = get(client, make_token(email="jules@example.com"),
               **{"Cf-Access-Authenticated-User-Email": "abel@example.com"})
    assert signed_in_as(resp) == "Jules"


@pytest.mark.parametrize("token", [
    pytest.param(make_token(key=OTHER_KEY), id="wrong-signing-key"),
    pytest.param(make_token(aud=["some-other-app"]), id="wrong-audience"),
    pytest.param(make_token(iss="https://evil.cloudflareaccess.com"), id="wrong-issuer"),
    pytest.param(make_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600),
                 id="expired"),
    pytest.param(make_token(exp=None), id="no-expiry"),
    pytest.param(make_token(email=None), id="no-email"),
    pytest.param(jwt.encode({"email": "abel@example.com", "aud": [AUD],
                             "iss": f"https://{TEAM}", "iat": int(time.time()),
                             "exp": int(time.time()) + 3600}, None, algorithm="none"),
                 id="alg-none"),
    pytest.param("not-a-jwt", id="garbage"),
])
def test_bad_tokens_are_rejected(client, token):
    assert get(client, token).status_code == 401


def test_valid_token_for_unknown_user_is_forbidden(client):
    assert get(client, make_token(email="stranger@example.com")).status_code == 403


def test_healthz_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_dev_user_bypasses_access(tmp_path):
    settings = Settings(users=USERS, dev_user="jules@example.com", data_dir=str(tmp_path))
    resp = TestClient(create_app(settings)).get("/")
    assert resp.status_code == 200
    assert signed_in_as(resp) == "Jules"


def test_dev_user_refused_alongside_production_access_config():
    settings = Settings(access_team_domain=TEAM, access_aud=AUD, users=USERS,
                        dev_user="abel@example.com")
    with pytest.raises(ConfigError):
        settings.validate()


def test_access_config_required_without_dev_user():
    with pytest.raises(ConfigError):
        Settings(users=USERS).validate()


def test_parse_users():
    assert _parse_users(" A@x.com = Abel , j@x.com=Jules ") == {
        "a@x.com": "Abel", "j@x.com": "Jules"}
    with pytest.raises(ConfigError):
        _parse_users("no-equals-sign")
