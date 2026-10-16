import hashlib
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.gifts import format_price

STATIC = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["price"] = lambda cents: f"${format_price(cents)}" if cents is not None else ""


@cache
def static_url(name: str) -> str:
    """/static/<name>?v=<content hash>. A deploy that changes a file changes its URL,
    so no browser or Cloudflare cache can pair a new page with an old script."""
    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = static_url
