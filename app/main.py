import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import logins
from app.auth import AccessMiddleware, AccessVerifier
from app.config import Settings, load_settings
from app.csrf import CSRFMiddleware
from app.db import make_engine, make_sessionmaker, migrate
from app.gifts import ensure_occasions
from app.photos import PhotoStore
from app.routes import (account, budget, gallery, gifts, photos, pwa,
                        settings as settings_routes, welcome)
from app.seed import seed
from app.visibility import delete_orphan_photos

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None, verifier: AccessVerifier | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.validate()

    if settings.dev_user:
        log.warning("*** DEV_USER is set: sign-in is BYPASSED (acting as %s) ***",
                    settings.dev_user)
    elif settings.auth_mode == "cloudflare" and verifier is None:
        verifier = AccessVerifier(settings)

    engine = make_engine(settings.data_dir)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    store = PhotoStore(settings.data_dir)
    with sessions() as session:
        if seed(session):
            log.info("Seeded empty database")
        if made := logins.bootstrap_users(session, settings.users):
            log.info("Created %d app user(s) from APP_USERS", made)
        elif not logins.app_users(session):
            log.warning("No app users yet. %s", "Open the app to create the first login."
                        if settings.auth_mode == "password" else
                        "Set APP_USERS, or run python -m app.users invite NAME --email EMAIL")
        ensure_occasions(session, date.today())
        delete_orphan_photos(session, store, datetime.now(timezone.utc) - timedelta(days=1))

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.sessionmaker = sessions
    app.state.photo_store = store
    app.state.login_throttle = logins.Throttle()
    app.add_middleware(AccessMiddleware, settings=settings, verifier=verifier,
                       sessionmaker=sessions)
    app.add_middleware(CSRFMiddleware)  # outermost: runs first
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    for module in (account, gallery, gifts, photos, pwa, settings_routes, budget, welcome):
        app.include_router(module.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
