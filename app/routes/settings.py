"""Settings: budget rules, people and relations, groups, occasion types, app users,
backups.

Every POST redirects back to its tab with a short notice, or re-renders the tab
with the error (422). Budgets aimed at the viewer 404 like hidden gifts.
"""

import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import backup, logins, manage
from app.auth import CurrentUser, current_user
from app.deps import db_session, gift_repo
from app.gifts import FormError, ensure_occasions, format_price
from app.models import BudgetRule, Gift, Group, OccasionType, Person, Relation
from app.routes.account import public_url
from app.templating import templates
from app.visibility import GiftRepo

router = APIRouter(prefix="/settings")

NOTICES = {"saved": "Saved.", "added": "Added.", "deleted": "Deleted.",
           "removed": "Login removed. They've been signed out everywhere.",
           "password": "Password changed. Your other devices have been signed out.",
           "requested": "Backing up now. It takes a minute or so; reload this page to see it."}
TABS = {"budgets": "Budgets", "people": "People", "relations": "Relations", "groups": "Groups",
        "occasions": "Occasions", "users": "App users", "backups": "Backups"}


def done(tab: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"/settings/{tab}?done={notice}", status_code=303)


def people_and_relations(session: Session) -> tuple[list[Person], list[Relation]]:
    people = list(session.scalars(select(Person).order_by(Person.name)))
    relations = manage.relations(session)
    return people, relations


def tab_context(tab: str, session: Session, viewer_id: int) -> dict:
    people, relations = people_and_relations(session)
    types = list(session.scalars(select(OccasionType).order_by(OccasionType.id)))
    context = {"tab": tab, "tabs": TABS, "viewer_id": viewer_id, "people": people,
               "relations": relations, "occasion_types": types, "months": manage.MONTHS}
    if tab == "budgets":
        rules = sorted(manage.visible_rules(session, viewer_id), key=manage.rule_sort_key)
        groups = [g for g in session.scalars(select(Group).where(~Group.ad_hoc).order_by(Group.name))
                  if viewer_id not in {m.id for m in g.members}]
        context.update(
            rules_by_type={t.id: [r for r in rules if r.occasion_type_id == t.id] for t in types},
            label=manage.rule_target_label, price=format_price,
            target_people=[p for p in people if p.active and p.id != viewer_id],
            target_groups=groups)
    elif tab == "groups":
        context["groups"] = list(session.scalars(
            select(Group).where(~Group.ad_hoc).order_by(Group.name)))
    elif tab == "users":
        users = logins.app_users(session)
        context.update(
            users=users, pending={u.id: logins.pending_invite(session, u.id) for u in users},
            candidates=[p for p in people if p.active and p.login_email is None])
    return context


def backups_context(data_dir: str) -> dict:
    status = backup.read_status(Path(data_dir))
    service = backup.service_state(Path(data_dir))
    return {"status": status, "last_ok": backup.last_good(status), "service": service,
            "pending": bool(service) and backup.requested(Path(data_dir)),
            "when": friendly_time, "clock": clock, "megabytes": megabytes}


def local_time(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso).astimezone()  # the backup service's TZ may differ
    except (TypeError, ValueError):
        return None


def clock(iso: str | None) -> str:
    at = local_time(iso)
    return f"{at:%-I:%M %p}".lower() if at else "unknown"


def friendly_time(iso: str | None) -> str:
    at = local_time(iso)
    if at is None:
        return "unknown"
    days = (date.today() - at.date()).days
    day = {0: "today", 1: "yesterday"}.get(days, f"{at:%a %b} {at.day}")
    return f"{day} at {clock(iso)}"


def megabytes(size) -> str:
    return f"{(size or 0) / 1_000_000:.1f} MB"


def render(request: Request, tab: str, session: Session, viewer_id: int, *,
           error: str | None = None, done_key: str | None = None, extra: dict | None = None):
    context = tab_context(tab, session, viewer_id)
    settings = request.app.state.settings
    if tab == "backups":
        context.update(backups_context(settings.data_dir))
    context.update(error=error, notice=NOTICES.get(done_key or ""),
                   password_mode=settings.auth_mode == "password" and not settings.dev_user,
                   **(extra or {}))
    return templates.TemplateResponse(request, f"settings_{tab}.html", context,
                                      status_code=422 if error else 200)


def attempt(request: Request, tab: str, session: Session, viewer_id: int, notice: str, action):
    """Run action(); commit and redirect, or roll back and show the error.

    Rows save as you go (add.js posts them with X-Autosave); those get a small
    JSON answer instead of a page."""
    autosave = request.headers.get("x-autosave") == "1"
    try:
        action()
        session.commit()
    except FormError as exc:
        session.rollback()
        if autosave:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return render(request, tab, session, viewer_id, error=str(exc))
    return JSONResponse({"ok": True}) if autosave else done(tab, notice)


@router.get("", include_in_schema=False)
def settings_home():
    return RedirectResponse("/settings/budgets", status_code=303)


@router.get("/{tab}", response_class=HTMLResponse)
def settings_tab(request: Request, tab: str, done: str | None = None,
                 session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    if tab not in TABS:
        raise HTTPException(404)
    return render(request, tab, session, repo.viewer_id, done_key=done)


# --- budgets -------------------------------------------------------------------------

@router.post("/budgets")
async def add_rule(request: Request, session: Session = Depends(db_session),
                   repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()

    def action():
        occasion_type = session.get(OccasionType, int(form.get("occasion_type_id") or 0))
        if occasion_type is None:
            raise FormError("Unknown occasion")
        target = manage.parse_target(session, form.get("target"), repo.viewer_id)
        basis = form.get("basis") if target["group_id"] else "per_member"
        amount = manage.parse_amount(form.get("amount"))
        manage.check_rule(session, occasion_type=occasion_type, basis=basis, **target)
        session.add(BudgetRule(occasion_type=occasion_type, amount_cents=amount, basis=basis,
                               **target))

    return attempt(request, "budgets", session, repo.viewer_id, "added", action)


def visible_rule_or_404(session: Session, repo: GiftRepo, rule_id: int) -> BudgetRule:
    rule = manage.visible_rule(session, repo.viewer_id, rule_id)
    if rule is None:
        raise HTTPException(404)
    return rule


@router.post("/budgets/{rule_id}")
async def update_rule(request: Request, rule_id: int, session: Session = Depends(db_session),
                      repo: GiftRepo = Depends(gift_repo)):
    rule = visible_rule_or_404(session, repo, rule_id)
    form = await request.form()

    def action():
        rule.amount_cents = manage.parse_amount(form.get("amount"))
        if rule.group_id:
            basis = form.get("basis") or rule.basis
            manage.check_rule(session, occasion_type=rule.occasion_type, group_id=rule.group_id,
                              basis=basis, rule_id=rule.id)
            rule.basis = basis

    return attempt(request, "budgets", session, repo.viewer_id, "saved", action)


@router.post("/budgets/{rule_id}/delete")
def delete_rule(rule_id: int, session: Session = Depends(db_session),
                repo: GiftRepo = Depends(gift_repo)):
    session.delete(visible_rule_or_404(session, repo, rule_id))
    session.commit()
    return done("budgets", "deleted")


# --- people and relations ------------------------------------------------------------------

@router.post("/people")
async def add_person(request: Request, session: Session = Depends(db_session),
                     repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()
    return attempt(request, "people", session, repo.viewer_id, "added",
                   lambda: session.add(Person(**manage.person_fields(session, form))))


@router.post("/people/{person_id}")
async def update_person(request: Request, person_id: int, session: Session = Depends(db_session),
                        repo: GiftRepo = Depends(gift_repo)):
    person = session.get(Person, person_id) or _404()
    form = await request.form()

    def action():
        fields = manage.person_fields(session, form, person_id)
        if person.login_email and not fields["active"]:
            raise FormError(f"{person.name} uses the app, so can't be made inactive")
        for key, value in fields.items():
            setattr(person, key, value)

    return attempt(request, "people", session, repo.viewer_id, "saved", action)


@router.post("/relations")
async def add_relation(request: Request, session: Session = Depends(db_session),
                       repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()
    return attempt(request, "relations", session, repo.viewer_id, "added",
                   lambda: session.add(manage.new_relation(session, manage.relation_name(session, form))))


@router.post("/relations/{relation_id}")
async def rename_relation(request: Request, relation_id: int,
                          session: Session = Depends(db_session),
                          repo: GiftRepo = Depends(gift_repo)):
    relation = session.get(Relation, relation_id) or _404()
    form = await request.form()

    def action():
        relation.name = manage.relation_name(session, form, relation_id)

    return attempt(request, "relations", session, repo.viewer_id, "saved", action)


@router.post("/relations/{relation_id}/up")
@router.post("/relations/{relation_id}/down")
def move_relation(request: Request, relation_id: int, session: Session = Depends(db_session),
                  repo: GiftRepo = Depends(gift_repo)):
    relation = session.get(Relation, relation_id) or _404()
    manage.move_relation(session, relation, -1 if request.url.path.endswith("/up") else 1)
    session.commit()
    return RedirectResponse(f"/settings/relations#relation-{relation.id}", status_code=303)


@router.post("/relations/{relation_id}/delete")
def delete_relation(request: Request, relation_id: int, session: Session = Depends(db_session),
                    repo: GiftRepo = Depends(gift_repo)):
    relation = session.get(Relation, relation_id) or _404()

    def action():
        if manage.relation_in_use(session, relation):
            raise FormError(f"{relation.name} is used by people or budgets, so it can't be "
                            f"deleted. Rename it instead.")
        session.delete(relation)

    return attempt(request, "relations", session, repo.viewer_id, "deleted", action)


# --- groups ----------------------------------------------------------------------------------

@router.post("/groups")
async def add_group(request: Request, session: Session = Depends(db_session),
                    repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()

    def action():
        name, members = manage.group_fields(session, form)
        session.add(Group(name=name, members=members))

    return attempt(request, "groups", session, repo.viewer_id, "added", action)


@router.post("/groups/{group_id}")
async def update_group(request: Request, group_id: int, session: Session = Depends(db_session),
                       repo: GiftRepo = Depends(gift_repo)):
    group = session.get(Group, group_id)
    if group is None or group.ad_hoc:
        raise HTTPException(404)
    form = await request.form()

    def action():
        name, members = manage.group_fields(session, form, group)
        manage.check_one_group_budget(session, group, {m.id for m in members})
        group.name, group.members = name, members

    return attempt(request, "groups", session, repo.viewer_id, "saved", action)


@router.post("/groups/{group_id}/delete")
def delete_group(request: Request, group_id: int, session: Session = Depends(db_session),
                 repo: GiftRepo = Depends(gift_repo)):
    group = session.get(Group, group_id)
    if group is None or group.ad_hoc:
        raise HTTPException(404)

    def action():
        # Gifts to a group the viewer is in are hidden from them, so "it has gifts"
        # would be a spoiler: refuse those groups whatever their gifts.
        if repo.viewer_id in {m.id for m in group.members}:
            raise FormError("Groups that include you can't be deleted here")
        if repo.count(Gift.recipient_group_id == group.id):
            raise FormError(f"Gifts are recorded for {group.name}, so it can't be deleted. "
                            f"Rename it or change its members instead.")
        session.delete(group)

    return attempt(request, "groups", session, repo.viewer_id, "deleted", action)


# --- occasion types -----------------------------------------------------------------------------

@router.post("/occasions")
async def add_occasion_type(request: Request, session: Session = Depends(db_session),
                            repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()

    def action():
        fields = manage.occasion_type_fields(session, form)
        if fields["date_rule"] == "birthday":
            raise FormError("There's already a Birthday occasion")
        session.add(OccasionType(**fields))
        session.flush()
        ensure_occasions(session, date.today())  # "Anniversary 2026" and 2027 exist at once

    return attempt(request, "occasions", session, repo.viewer_id, "added", action)


@router.post("/occasions/{type_id}")
async def update_occasion_type(request: Request, type_id: int,
                               session: Session = Depends(db_session),
                               repo: GiftRepo = Depends(gift_repo)):
    occasion_type = session.get(OccasionType, type_id) or _404()
    form = await request.form()

    def action():
        fields = manage.occasion_type_fields(session, form, type_id)
        if occasion_type.date_rule == "birthday" and fields["date_rule"] != "birthday":
            raise FormError("Birthday's date comes from each person's birthday; "
                            "it can be renamed but not re-dated")
        if fields["date_rule"] == "birthday" and occasion_type.date_rule != "birthday":
            raise FormError("Only the Birthday occasion uses people's birthdays")
        for key, value in fields.items():
            setattr(occasion_type, key, value)

    return attempt(request, "occasions", session, repo.viewer_id, "saved", action)


# --- app users ---------------------------------------------------------------------------------

def show_invite(request: Request, session: Session, viewer_id: int, person: Person, token: str,
                fresh: bool):
    """The tab again, with this person's link to copy. Shown once: only its hash is kept."""
    return render(request, "users", session, viewer_id, extra={
        "invite_for": person, "invite_fresh": fresh,
        "invite_link": public_url(request, f"/invite/{token}")})


@router.post("/users")
async def give_login(request: Request, session: Session = Depends(db_session),
                     repo: GiftRepo = Depends(gift_repo)):
    form = await request.form()
    person = session.get(Person, int(form.get("person_id") or 0))
    if person is None or person.login_email is not None:
        return render(request, "users", session, repo.viewer_id, error="Choose someone")
    try:
        logins.give_login(session, person, form.get("email"))
        session.flush()
    except FormError as exc:
        session.rollback()
        return render(request, "users", session, repo.viewer_id, error=str(exc))
    if request.app.state.settings.auth_mode == "password":
        token = logins.create_invite(session, person, repo.viewer_id)
        session.commit()
        return show_invite(request, session, repo.viewer_id, person, token, fresh=True)
    session.commit()
    return render(request, "users", session, repo.viewer_id, extra={"cf_added": person})


@router.post("/users/{person_id}/invite")
def new_invite(request: Request, person_id: int, session: Session = Depends(db_session),
               repo: GiftRepo = Depends(gift_repo)):
    person = session.get(Person, person_id)
    if person is None or person.login_email is None:
        raise HTTPException(404)
    if request.app.state.settings.auth_mode != "password":
        raise HTTPException(404)
    token = logins.create_invite(session, person, repo.viewer_id)
    session.commit()
    return show_invite(request, session, repo.viewer_id, person, token, fresh=False)


@router.post("/users/{person_id}/remove")
def remove_login(request: Request, person_id: int, session: Session = Depends(db_session),
                 repo: GiftRepo = Depends(gift_repo)):
    person = session.get(Person, person_id)
    if person is None or person.login_email is None:
        raise HTTPException(404)

    def action():
        logins.remove_login(session, person, repo.viewer_id)

    return attempt(request, "users", session, repo.viewer_id, "removed", action)


# --- backups -------------------------------------------------------------------------------

@router.post("/backups/now")
def back_up_now(request: Request, session: Session = Depends(db_session),
                repo: GiftRepo = Depends(gift_repo)):
    """Ask the backup service (a separate container) to run; it checks every few seconds."""
    data_dir = Path(request.app.state.settings.data_dir)
    if backup.service_state(data_dir) is None:
        return render(request, "backups", session, repo.viewer_id,
                      error="The backup service isn't running, so it can't back up now")
    backup.request_backup(data_dir)
    return done("backups", "requested")


@router.post("/backups/download")
def download_backup(request: Request, repo: GiftRepo = Depends(gift_repo)):
    """The whole database and photos, made fresh. It holds every gift, the viewer's
    own included; the page warns about that before the download starts."""
    work = Path(tempfile.mkdtemp(prefix="lumpofcoal-download-"))
    name = backup.archive_name(datetime.now())
    try:
        backup.write_archive(Path(request.app.state.settings.data_dir), work / name)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(work / name, media_type="application/gzip", filename=name,
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))


@router.post("/password")
async def change_password(request: Request, session: Session = Depends(db_session),
                          user: CurrentUser = Depends(current_user)):
    if request.app.state.settings.auth_mode != "password":
        raise HTTPException(404)
    form = await request.form()
    me = session.get(Person, user.person_id)
    try:
        if not logins.check_password(str(form.get("current") or ""), me.password_hash):
            raise FormError("Your current password isn't right")
        logins.check_new_password(str(form.get("password") or ""), str(form.get("again") or ""))
    except FormError as exc:
        return render(request, "users", session, user.person_id, error=str(exc))
    me.password_hash = logins.hash_password(str(form.get("password")))
    logins.end_all_sessions(session, me.id, keep_token=request.cookies.get(logins.COOKIE))
    session.commit()
    return done("users", "password")


def _404():
    raise HTTPException(404)
