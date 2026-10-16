"""Signing in and out, invite links, and changing your password (AUTH_MODE=password).

In Cloudflare mode, Access handles all of this, so these pages 404.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import logins
from app.deps import db_session
from app.gifts import FormError
from app.models import Person, Relation
from app.templating import templates

router = APIRouter()


def password_mode(request: Request) -> None:
    settings = request.app.state.settings
    if settings.auth_mode != "password" or settings.dev_user:
        raise HTTPException(404)


def is_https(request: Request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")


def public_url(request: Request, path: str) -> str:
    """An absolute link to this app, as the browser sees it (behind a proxy too)."""
    scheme = "https" if is_https(request) else request.url.scheme
    return f"{scheme}://{request.url.netloc}{path}"


def signed_in(request: Request, token: str, target: str = "/") -> RedirectResponse:
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(logins.COOKIE, token, max_age=logins.SESSION_DAYS * 86400, httponly=True,
                    secure=is_https(request), samesite="lax", path="/")
    return resp


def safe_next(target: str | None) -> str:
    """Only paths on this site: never //evil.example or /\\evil.example."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


def client_ip(request: Request) -> str:
    # cloudflared or a reverse proxy usually sits in front; fall back to the socket.
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    return (forwarded or (request.client.host if request.client else "")).split(",")[0].strip()


# --- sign in / out --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse, dependencies=[Depends(password_mode)])
def login_page(request: Request, next: str | None = None, out: str | None = None):
    return templates.TemplateResponse(request, "login.html", {
        "next": safe_next(next), "email": "", "error": None,
        "notice": "Signed out." if out else None})


@router.post("/login", dependencies=[Depends(password_mode)])
async def login(request: Request, session: Session = Depends(db_session)):
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    target = safe_next(form.get("next"))
    throttle: logins.Throttle = request.app.state.login_throttle
    keys = (f"email:{email}", f"ip:{client_ip(request)}")

    def again(error: str, status: int):
        return templates.TemplateResponse(request, "login.html", {
            "next": target, "email": email, "error": error, "notice": None}, status_code=status)

    if throttle.blocked(*keys):
        return again("Too many tries. Wait a few minutes, then try again.", 429)
    person = session.scalar(select(Person).where(func.lower(Person.login_email) == email)) \
        if email else None
    if not logins.check_password(password, person.password_hash if person else None):
        throttle.failed(*keys)
        return again("That email and password don't match.", 401)
    throttle.clear(*keys)
    token = logins.start_session(session, person)
    session.commit()
    return signed_in(request, token, target)


@router.post("/logout", dependencies=[Depends(password_mode)])
def logout(request: Request, session: Session = Depends(db_session)):
    logins.end_session(session, request.cookies.get(logins.COOKIE))
    resp = RedirectResponse("/login?out=1", status_code=303)
    resp.delete_cookie(logins.COOKIE, path="/")
    return resp


# --- invites --------------------------------------------------------------------------------

def gone(request: Request):
    return templates.TemplateResponse(request, "invite.html", {"person": None, "error": None},
                                      status_code=404)


@router.get("/invite/{token}", response_class=HTMLResponse, dependencies=[Depends(password_mode)])
def invite_page(request: Request, token: str, session: Session = Depends(db_session)):
    invite = logins.open_invite(session, token)
    if invite is None:
        return gone(request)
    return templates.TemplateResponse(request, "invite.html", {
        "person": invite.person, "error": None})


@router.post("/invite/{token}", dependencies=[Depends(password_mode)])
async def accept_invite(request: Request, token: str, session: Session = Depends(db_session)):
    invite = logins.open_invite(session, token)
    if invite is None:
        return gone(request)
    form = await request.form()
    password = str(form.get("password") or "")
    try:
        logins.check_new_password(password, str(form.get("again") or ""))
    except FormError as exc:
        return templates.TemplateResponse(request, "invite.html", {
            "person": invite.person, "error": str(exc)}, status_code=422)
    return signed_in(request, logins.accept_invite(session, invite, password))


# --- first run -----------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse, dependencies=[Depends(password_mode)])
def setup_page(request: Request, session: Session = Depends(db_session)):
    if logins.app_users(session):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"form": {}, "error": None})


@router.post("/setup", dependencies=[Depends(password_mode)])
async def setup(request: Request, session: Session = Depends(db_session)):
    """The first login on a brand-new copy. Only while nobody has a login."""
    if logins.app_users(session):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    password = str(form.get("password") or "")
    try:
        if not name:
            raise FormError("Enter your name")
        logins.check_new_password(password, str(form.get("again") or ""))
        person = session.scalar(select(Person).where(func.lower(Person.name) == name.lower()))
        if person is None:
            # Your partner (if any) will see you as their Partner.
            partner = session.scalar(select(Relation).where(Relation.name == "Partner"))
            person = Person(name=name, relation=partner)
            session.add(person)
        logins.give_login(session, person, form.get("email"))
        person.password_hash = logins.hash_password(password)
        session.flush()
    except FormError as exc:
        session.rollback()
        return templates.TemplateResponse(request, "setup.html", {
            "form": {"name": name, "email": form.get("email") or ""}, "error": str(exc)},
            status_code=422)
    token = logins.start_session(session, person)
    session.commit()
    return signed_in(request, token, "/welcome")
