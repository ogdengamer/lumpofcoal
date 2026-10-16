"""App users and password sign-in (AUTH_MODE=password).

- Passwords: scrypt (Python's standard library), with a random salt per password.
- Sign-in: a random token in an HttpOnly cookie. The database stores only its
  SHA-256 (LoginSession), so removing a login signs that person out at once, and a
  copy of the database (a backup) can't be used to sign in. A session lasts a year
  from when it was last used.
- Invites: a one-time link, good for 7 days, to choose a password. It's how a new
  login starts and how a forgotten password is reset. Nothing is ever emailed: the
  app shows the link and someone sends it however they like.
"""

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.gifts import FormError
from app.models import Invite, LoginSession, Person

SESSION_DAYS = 365
INVITE_DAYS = 7
COOKIE = "loc_session"
MIN_PASSWORD = 8

# scrypt cost: ~50 ms and 16 MB per check.
N, R, P = 2 ** 14, 8, 1


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(when: datetime) -> datetime:
    """SQLite hands datetimes back without a timezone; they're stored as UTC."""
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# --- passwords -------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Same work as a real check, so timing doesn't reveal which emails exist.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=N, r=R, p=P)
        return False
    _, n, r, p, salt, digest = stored.split("$")
    got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                         n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(got, base64.b64decode(digest))


def check_new_password(password: str, again: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise FormError(f"Use at least {MIN_PASSWORD} characters")
    if password != again:
        raise FormError("The two passwords don't match")


# --- tokens --------------------------------------------------------------------------------

def new_token() -> tuple[str, str]:
    """(token for the user, hash for the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- sessions ---------------------------------------------------------------------------------

def start_session(session: Session, person: Person) -> str:
    token, hashed = new_token()
    session.add(LoginSession(token_hash=hashed, person_id=person.id,
                             expires_at=now() + timedelta(days=SESSION_DAYS)))
    return token


def session_person(session: Session, token: str | None) -> Person | None:
    """The app user signed in with this cookie, extending the year as it's used."""
    if not token:
        return None
    login = session.scalar(select(LoginSession).where(LoginSession.token_hash == token_hash(token)))
    if login is None or aware(login.expires_at) <= now():
        return None
    person = login.person
    if person.login_email is None or person.password_hash is None:
        return None
    if now() - aware(login.last_used_at) > timedelta(days=1):  # one write a day, not per click
        login.last_used_at = now()
        login.expires_at = now() + timedelta(days=SESSION_DAYS)
        session.commit()
    return person


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(token)))
        session.commit()


def end_all_sessions(session: Session, person_id: int, keep_token: str | None = None) -> None:
    stmt = delete(LoginSession).where(LoginSession.person_id == person_id)
    if keep_token:
        stmt = stmt.where(LoginSession.token_hash != token_hash(keep_token))
    session.execute(stmt)


# --- invites ----------------------------------------------------------------------------------

def create_invite(session: Session, person: Person, created_by_id: int | None) -> str:
    """A fresh link for this person; any earlier unused link stops working."""
    session.execute(delete(Invite).where(Invite.person_id == person.id, Invite.used_at.is_(None)))
    token, hashed = new_token()
    session.add(Invite(token_hash=hashed, person_id=person.id, created_by_id=created_by_id,
                       expires_at=now() + timedelta(days=INVITE_DAYS)))
    return token


def open_invite(session: Session, token: str) -> Invite | None:
    invite = session.scalar(select(Invite).where(Invite.token_hash == token_hash(token)))
    if (invite is None or invite.used_at is not None or aware(invite.expires_at) <= now()
            or invite.person.login_email is None):
        return None
    return invite


def pending_invite(session: Session, person_id: int) -> Invite | None:
    return session.scalar(select(Invite).where(
        Invite.person_id == person_id, Invite.used_at.is_(None), Invite.expires_at > now()))


def accept_invite(session: Session, invite: Invite, password: str) -> str:
    """Set the password, sign out the person's other devices, and sign this one in."""
    person = invite.person
    person.password_hash = hash_password(password)
    invite.used_at = now()
    end_all_sessions(session, person.id)
    token = start_session(session, person)
    session.commit()
    return token


# --- app users ------------------------------------------------------------------------------------

def normalise_email(raw: str | None) -> str:
    email = (raw or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise FormError("Enter an email address")
    return email


def app_users(session: Session) -> list[Person]:
    return list(session.scalars(select(Person).where(Person.login_email.is_not(None))
                                .order_by(Person.name)))


def give_login(session: Session, person: Person, raw_email: str | None) -> None:
    email = normalise_email(raw_email)
    taken = session.scalar(select(Person).where(func.lower(Person.login_email) == email,
                                                Person.id != person.id))
    if taken:
        raise FormError(f"{email} is already {taken.name}'s login")
    person.login_email = email
    person.active = True


def remove_login(session: Session, person: Person, viewer_id: int) -> None:
    if person.id == viewer_id:
        raise FormError("You can't remove your own login")
    if len(app_users(session)) <= 1:
        raise FormError("That's the last login; nobody could sign in")
    person.login_email = None
    person.password_hash = None
    end_all_sessions(session, person.id)
    session.execute(delete(Invite).where(Invite.person_id == person.id))


def bootstrap_users(session: Session, users: dict[str, str]) -> int:
    """APP_USERS, applied only while nobody has a login yet (a brand-new copy).

    After that, logins are managed in Settings > App users, and APP_USERS is ignored,
    so a restart never brings back a login someone removed. Returns how many were made.
    """
    if not users or app_users(session):
        return 0
    for email, name in users.items():
        person = session.scalar(select(Person).where(Person.name == name))
        if person is None:
            person = Person(name=name)
            session.add(person)
        person.login_email = email
    session.commit()
    return len(users)


# --- slowing down password guessing ------------------------------------------------------------------

class Throttle:
    """At most LIMIT failed sign-ins per email, and per address, in WINDOW seconds."""

    LIMIT, WINDOW = 5, 15 * 60

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures: dict[str, deque] = defaultdict(deque)

    def _recent(self, key: str) -> deque:
        q, cutoff = self.failures[key], self.clock() - self.WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._recent(k)) >= self.LIMIT for k in keys)

    def failed(self, *keys: str) -> None:
        for k in keys:
            self._recent(k).append(self.clock())

    def clear(self, *keys: str) -> None:
        for k in keys:
            self.failures.pop(k, None)
