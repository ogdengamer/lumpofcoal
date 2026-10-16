"""Who is signed in: Cloudflare Access, or the app's own password sign-in.

AUTH_MODE=cloudflare: every request reaching the app has come through cloudflared,
which adds the Cf-Access-Jwt-Assertion header. We verify that JWT ourselves
(signature, audience, issuer, expiry) and never trust
Cf-Access-Authenticated-User-Email, since anything that can reach the container
directly could forge it.

AUTH_MODE=password: a session cookie (see app/logins.py).

Either way the email must belong to an app user (Person.login_email), looked up on
every request, so removing a login in Settings takes effect at once.
"""

import logging
from dataclasses import dataclass
from typing import Callable

import jwt
from urllib.parse import quote

from fastapi import Request
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from app import logins
from app.config import Settings
from app.models import Person

log = logging.getLogger(__name__)

JWT_HEADER = "Cf-Access-Jwt-Assertion"

# Paths served without auth. Keep this list tiny and information-free.
# The PWA files are here because browsers fetch some of them without cookies
# (and Android's installer may fetch icons from outside the session).
PUBLIC_PATHS = {"/healthz", "/manifest.webmanifest", "/sw.js", "/offline"}
# Stylesheet, script and icons: no user data, and the sign-in page needs them.
PUBLIC_PREFIXES = ("/static/",)
# The sign-in pages themselves (password mode only).
SIGN_IN_PATHS = {"/login", "/setup"}
SIGN_IN_PREFIXES = ("/invite/",)


@dataclass(frozen=True)
class CurrentUser:
    email: str
    person_name: str
    person_id: int


class AuthError(Exception):
    pass


# Given a raw token, return the key that signed it.
KeyResolver = Callable[[str], object]


def jwks_key_resolver(certs_url: str) -> KeyResolver:
    client = jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=3600)
    return lambda token: client.get_signing_key_from_jwt(token).key


class AccessVerifier:
    def __init__(self, settings: Settings, resolve_key: KeyResolver | None = None):
        self.settings = settings
        self.resolve_key = resolve_key or jwks_key_resolver(settings.access_certs_url)

    def verify(self, token: str) -> str:
        """Return the verified, lower-cased email claim, or raise AuthError."""
        try:
            key = self.resolve_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.access_aud,
                issuer=self.settings.access_issuer,
                options={"require": ["exp", "iat", "aud", "iss", "email"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc
        return str(claims["email"]).strip().lower()


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings, verifier: AccessVerifier | None, sessionmaker):
        super().__init__(app)
        self.settings = settings
        self.verifier = verifier
        self.sessionmaker = sessionmaker

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or (path.startswith(PUBLIC_PREFIXES) and ".." not in path):
            return await call_next(request)
        password_mode = self.settings.auth_mode == "password" and not self.settings.dev_user
        if password_mode and (path in SIGN_IN_PATHS or path.startswith(SIGN_IN_PREFIXES)):
            return await call_next(request)

        with self.sessionmaker() as session:
            if self.settings.dev_user:
                person = self._by_email(session, self.settings.dev_user)
            elif password_mode:
                person = logins.session_person(session, request.cookies.get(logins.COOKIE))
                if person is None:
                    return self._sign_in(request, first_run=not logins.app_users(session))
            else:
                token = request.headers.get(JWT_HEADER)
                if not token:
                    return PlainTextResponse("Not authenticated", status_code=401)
                try:
                    email = self.verifier.verify(token)
                except AuthError as exc:
                    log.warning("Rejected Access JWT: %s", exc)
                    return PlainTextResponse("Not authenticated", status_code=401)
                person = self._by_email(session, email)
                if person is None:
                    log.warning("Valid Access login for unknown user %s", email)
            if person is None:
                return PlainTextResponse("Not authorized", status_code=403)
            request.state.user = CurrentUser(email=person.login_email, person_name=person.name,
                                             person_id=person.id)
        return await call_next(request)

    @staticmethod
    def _by_email(session, email: str) -> Person | None:
        return session.scalar(select(Person).where(Person.login_email == email.lower()))

    @staticmethod
    def _sign_in(request: Request, first_run: bool) -> Response:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            if first_run:  # a brand-new copy: create the first login
                return RedirectResponse("/setup", status_code=303)
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            next_part = "" if target == "/" else f"?next={quote(target, safe='')}"
            return RedirectResponse(f"/login{next_part}", status_code=303)
        return PlainTextResponse("Not signed in", status_code=401)


def current_user(request: Request) -> CurrentUser:
    """FastAPI dependency for routes that need the logged-in user."""
    return request.state.user
