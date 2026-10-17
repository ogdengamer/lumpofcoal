"""Adding, viewing, editing and deleting gifts, and the Android share target.

Every gift lookup goes through GiftRepo, so a gift addressed to the viewer 404s
on every route here exactly as if it didn't exist.
"""

import json
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile  # what request.form() returns

from app.deps import db_session, gift_repo
from app.gifts import (
    NEW_PERSON, FormError, default_occasion, find_or_add_person, find_or_create_group,
    format_price, group_budget_hints,
    known_sources, occasion_choices, parse_price, recipient_chips,
)
from app.models import STATUSES, Gift, Group, Occasion, Person
from app.photos import MAX_UPLOAD_BYTES, PhotoError, PhotoStore, process
from app.routes.photos import photo_store
from app.templating import templates
from app.visibility import GiftRepo

router = APIRouter()


# --- shared form context ------------------------------------------------------

def add_form_context(session: Session, repo: GiftRepo, values: dict | None = None) -> dict:
    """Everything the gift form needs. Every page includes the add form (in a
    dialog), so the "+" button can open the camera in a single tap."""
    today = date.today()
    top, rest = recipient_chips(session, repo)
    last = repo.last_created()
    occasion = default_occasion(session, repo, today)
    defaults = {
        "occasion_id": occasion.id if occasion else None,
        "source": last.source if last and last.source else "",
        "status": "bought",
    }
    return {
        "chips_top": top,
        "chips_rest": rest,
        "occasions": occasion_choices(session, today),
        "sources": known_sources(session, repo),
        "statuses": STATUSES,
        "hints_json": json.dumps(group_budget_hints(session, repo.viewer_id)),
        "add_form": {**defaults, **(values or {})},
    }


# --- parsing --------------------------------------------------------------------

def _resolve_recipient(session: Session, keys: list[str], group_name: str | None,
                       new_person: str, viewer_id: int) -> dict:
    """Chip keys ("person:3", "group:2", "new") -> both recipient fields for a Gift.

    "new" is the person typed into the form; they're created here, with the gift."""
    person_ids: set[int] = set()
    groups: list[Group] = []
    for key in keys:
        if key == NEW_PERSON:
            person_ids.add(find_or_add_person(session, new_person, viewer_id).id)
            continue
        kind, _, raw_id = key.partition(":")
        if kind not in ("person", "group") or not raw_id.isdigit():
            raise FormError("Unknown recipient")
        if kind == "person":
            if session.get(Person, int(raw_id)) is None:
                raise FormError("Unknown recipient")
            person_ids.add(int(raw_id))
        else:
            group = session.get(Group, int(raw_id))
            if group is None:
                raise FormError("Unknown recipient")
            groups.append(group)

    if not person_ids and not groups:
        raise FormError("Pick who it's for")
    if len(groups) == 1 and not person_ids:
        return {"recipient_person_id": None, "recipient_group_id": groups[0].id}
    for group in groups:  # mixing groups and people: combine their members
        person_ids |= {m.id for m in group.members}
    if len(person_ids) == 1:
        return {"recipient_person_id": person_ids.pop(), "recipient_group_id": None}
    group = find_or_create_group(session, person_ids, group_name)
    return {"recipient_person_id": None, "recipient_group_id": group.id}


@dataclass
class ParsedForm:
    values: dict          # as typed, for redisplay on error
    photo_id: str | None  # newly uploaded photo to attach


def read_form(form: FormData) -> ParsedForm:
    return ParsedForm(
        values={
            "recipients": form.getlist("recipients"),
            "group_name": (form.get("group_name") or "").strip(),
            "new_person": (form.get("new_person") or "").strip(),
            "price": (form.get("price") or "").strip(),
            "occasion_id": form.get("occasion_id") or None,
            "source": (form.get("source") or "").strip(),
            "title": (form.get("title") or "").strip(),
            "status": form.get("status") or "bought",
            "order_url": (form.get("order_url") or "").strip(),
            "notes": (form.get("notes") or "").strip(),
        },
        photo_id=form.get("photo_id") or None,
    )


def gift_fields(session: Session, repo: GiftRepo, parsed: ParsedForm) -> dict:
    """Validated Gift column values. Raises FormError."""
    values = parsed.values
    occasion_id = None
    if values["occasion_id"]:
        occasion = session.get(Occasion, int(values["occasion_id"]))
        if occasion is None:
            raise FormError("Unknown occasion")
        occasion_id = occasion.id
    if values["status"] not in STATUSES:
        raise FormError("Unknown status")
    order_url = parse_order_url(values["order_url"])
    if parsed.photo_id and not repo.photo_visible(parsed.photo_id):
        raise FormError("That photo is no longer available; please take it again")
    return {
        **_resolve_recipient(session, values["recipients"], values["group_name"],
                             values["new_person"], repo.viewer_id),
        "cost_cents": parse_price(values["price"]),
        "occasion_id": occasion_id,
        "title": values["title"] or None,
        "source": values["source"] or None,
        "status": values["status"],
        "order_url": order_url,
        "notes": values["notes"] or None,
    }


def parse_order_url(text: str) -> str | None:
    """A web link, as pasted from a shop's app ("amazon.com/..." gets https://)."""
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc or " " in text:
        raise FormError("The order link doesn't look like a web address")
    if len(text) > 500:
        raise FormError("The order link is too long (500 characters at most)")
    return text


# --- add ---------------------------------------------------------------------------

def render_new(request: Request, session: Session, repo: GiftRepo, *, values=None,
               error=None, status_code=200, photo_id=None):
    context = add_form_context(session, repo, values)
    context.update(error=error, photo_id=photo_id, standalone=True)
    return templates.TemplateResponse(request, "gift_new.html", context,
                                      status_code=status_code)


@router.get("/gifts/new", response_class=HTMLResponse)
def new_gift(request: Request, photo: str | None = None,
             session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    photo_id = photo if photo and repo.photo_visible(photo) else None
    return render_new(request, session, repo, photo_id=photo_id)


@router.post("/gifts")
async def create_gift(request: Request, session: Session = Depends(db_session),
                      repo: GiftRepo = Depends(gift_repo)):
    parsed = read_form(await request.form())
    try:
        gift = repo.create(**gift_fields(session, repo, parsed))
        if parsed.photo_id:
            repo.attach_photo(gift.id, parsed.photo_id)
        session.commit()
    except (FormError, ValueError) as exc:
        session.rollback()
        return render_new(request, session, repo, values=parsed.values, error=str(exc),
                          status_code=422, photo_id=parsed.photo_id)
    return RedirectResponse(f"/?saved={gift.id}", status_code=303)


# --- view / edit / delete --------------------------------------------------------------

def values_from_gift(gift: Gift) -> dict:
    """Form values for an existing gift.

    Ad hoc groups have no chip of their own, so they show as their members."""
    group = gift.recipient_group
    if gift.recipient_person_id:
        recipients = [f"person:{gift.recipient_person_id}"]
    elif group and group.ad_hoc:
        recipients = [f"person:{m.id}" for m in group.members]
    else:
        recipients = [f"group:{gift.recipient_group_id}"]
    return {
        "recipients": recipients,
        "price": format_price(gift.cost_cents),
        "occasion_id": gift.occasion_id,
        "source": gift.source or "",
        "title": gift.title or "",
        "status": gift.status,
        "order_url": gift.order_url or "",
        "notes": gift.notes or "",
    }


def visible_gift(repo: GiftRepo, gift_id: int) -> Gift:
    gift = repo.get(gift_id)
    if gift is None:
        raise HTTPException(404)
    return gift


def render_detail(request: Request, session: Session, repo: GiftRepo, gift: Gift, *,
                  values=None, error=None, status_code=200, photo_id=None,
                  back=""):
    if values is None:
        values = values_from_gift(gift)
    context = add_form_context(session, repo)
    # standalone: the page is its own form, so no + button or add dialog over it.
    context.update(gift=gift, edit_form=values, error=error, back_href=back_href(back),
                   photo_id=photo_id, back=back, standalone=True)
    return templates.TemplateResponse(request, "gift_detail.html", context,
                                      status_code=status_code)


ATTENTION = "attention"  # back=attention: return to the needs-attention page


def safe_back(back: str | None) -> str:
    """A gallery query string to return to; never a URL (no open redirects)."""
    back = back or ""
    return back if "/" not in back and ":" not in back else ""


def back_href(back: str) -> str:
    if back == ATTENTION:
        return "/attention"
    return "/?" + back if back else "/"


@router.get("/gifts/{gift_id}", response_class=HTMLResponse)
def gift_detail(request: Request, gift_id: int, back: str = "",
                session: Session = Depends(db_session), repo: GiftRepo = Depends(gift_repo)):
    return render_detail(request, session, repo, visible_gift(repo, gift_id),
                         back=safe_back(back))


@router.post("/gifts/{gift_id}")
async def update_gift(request: Request, gift_id: int, session: Session = Depends(db_session),
                      repo: GiftRepo = Depends(gift_repo)):
    visible_gift(repo, gift_id)
    form = await request.form()
    parsed = read_form(form)
    back = safe_back(form.get("back"))
    try:
        repo.update(gift_id, **gift_fields(session, repo, parsed))
        for photo_id in form.getlist("remove_photo"):
            repo.detach_photo(gift_id, photo_id)
        if parsed.photo_id:
            repo.attach_photo(gift_id, parsed.photo_id)
        session.commit()
    except (FormError, ValueError) as exc:
        session.rollback()
        return render_detail(request, session, repo, visible_gift(repo, gift_id),
                             values=parsed.values,
                             error=str(exc), status_code=422, photo_id=parsed.photo_id,
                             back=back)
    if repo.get(gift_id) is None:  # e.g. re-addressed to the viewer: now hidden from them
        return RedirectResponse("/", status_code=303)
    # Back to the list as it was (same filters), with a "Saved …" banner.
    if back == ATTENTION:
        return RedirectResponse(f"/attention?saved={gift_id}", status_code=303)
    return RedirectResponse(f"/?{back + '&' if back else ''}saved={gift_id}&edited=1",
                            status_code=303)


@router.post("/gifts/{gift_id}/delete")
async def delete_gift(request: Request, gift_id: int, session: Session = Depends(db_session),
                      repo: GiftRepo = Depends(gift_repo)):
    back = safe_back((await request.form()).get("back"))
    # Its photos become unattached and are cleaned up by delete_orphan_photos.
    if not repo.delete(gift_id):
        raise HTTPException(404)
    session.commit()
    # Back to the list it was opened from, like saving does.
    if back == ATTENTION:
        return RedirectResponse("/attention?deleted=1", status_code=303)
    return RedirectResponse(f"/?{back + '&' if back else ''}deleted=1", status_code=303)


# --- share target ------------------------------------------------------------------------

@router.post("/share")
async def share_target(request: Request, repo: GiftRepo = Depends(gift_repo),
                       store: PhotoStore = Depends(photo_store)):
    """Android Web Share Target: a photo shared from the gallery starts a new gift."""
    form = await request.form()
    upload = next((f for f in form.getlist("photo") if isinstance(f, UploadFile)), None)
    if upload is None:
        return RedirectResponse("/gifts/new", status_code=303)
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    try:
        processed = await run_in_threadpool(process, data)
    except PhotoError:
        return RedirectResponse("/gifts/new", status_code=303)
    photo_id = store.new_id()
    store.save(photo_id, processed)
    repo.add_photo(photo_id)
    repo.session.commit()
    return RedirectResponse(f"/gifts/new?photo={photo_id}", status_code=303)
