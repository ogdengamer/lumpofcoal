"""Photo upload and serving. Serving goes through GiftRepo.photo_visible."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.deps import gift_repo
from app.photos import MAX_UPLOAD_BYTES, PhotoError, PhotoStore, process
from app.visibility import GiftRepo, delete_orphan_photos

router = APIRouter()

# Uploads not attached to a gift by then are from abandoned forms.
ORPHAN_TTL = timedelta(days=1)

# Extensionless URLs plus "private": Cloudflare's default edge cache keys on file
# extensions (.webp included), and a shared cache must never hold these.
CACHE_HEADERS = {"Cache-Control": "private, max-age=604800, immutable",
                 "X-Content-Type-Options": "nosniff"}


def photo_store(request: Request) -> PhotoStore:
    return request.app.state.photo_store


def photo_urls(photo_id: str) -> dict[str, str]:
    return {"full": f"/photos/{photo_id}/full", "thumb": f"/photos/{photo_id}/thumb"}


@router.post("/photos", status_code=201)
async def upload_photo(file: UploadFile, repo: GiftRepo = Depends(gift_repo),
                       store: PhotoStore = Depends(photo_store)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Photo is too large")
    try:
        processed = await run_in_threadpool(process, data)
    except PhotoError as exc:
        raise HTTPException(415, str(exc)) from exc

    photo_id = store.new_id()
    store.save(photo_id, processed)
    try:
        repo.add_photo(photo_id)
        repo.session.commit()
    except Exception:
        store.delete(photo_id)
        raise
    delete_orphan_photos(repo.session, store, datetime.now(timezone.utc) - ORPHAN_TTL)
    return {"id": photo_id, **photo_urls(photo_id)}


@router.get("/photos/{photo_id}/{size}")
def get_photo(photo_id: str, size: str, repo: GiftRepo = Depends(gift_repo),
              store: PhotoStore = Depends(photo_store)):
    try:
        path = store.path(photo_id, size)
    except ValueError:
        raise HTTPException(404)
    # Hidden and nonexistent photos look identical.
    if not repo.photo_visible(photo_id) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp", headers=CACHE_HEADERS)
