"""Reject cross-site form posts (CSRF).

Browsers label every request with Sec-Fetch-Site. State-changing requests must
come from our own pages ("same-origin") or be browser-initiated ("none", e.g. the
Android share sheet posting into the installed app). Requests without the header
come from non-browser clients, which can't ride on a victim's Access cookie.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_SITES = {"same-origin", "none"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        site = request.headers.get("sec-fetch-site")
        if request.method not in SAFE_METHODS and site is not None and site not in ALLOWED_SITES:
            return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)
