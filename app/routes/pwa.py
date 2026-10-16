"""Installable-app plumbing: manifest (with the Android share target), service
worker, offline page. These are public (see auth.PUBLIC_PATHS) and contain no
user data."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.templating import templates

router = APIRouter()

SW_PATH = Path(__file__).parent.parent / "static" / "sw.js"

MANIFEST = {
    "name": "Lump of Coal",
    "short_name": "Gifts",
    "description": "Gifts bought ahead, and what's left in the budget.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#1c1917",
    "theme_color": "#1c1917",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
    "share_target": {
        "action": "/share",
        "method": "POST",
        "enctype": "multipart/form-data",
        "params": {"files": [{"name": "photo", "accept": ["image/*", ".heic", ".heif"]}]},
    },
}


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    return FileResponse(SW_PATH, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html")
