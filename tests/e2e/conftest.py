"""Shared browser-test fixtures: the real app (dev-mode login as Abel) in a
background thread, driven by headless Chromium on an emulated Pixel 7.

Skipped when Playwright or its browser isn't installed:
  .venv/bin/python -m playwright install chromium
"""

import io
import socket
import threading
import time

import pytest
import uvicorn
from PIL import Image

from app.config import Settings
from app.main import create_app

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binary or system libs missing
            pytest.skip(f"Chromium unavailable: {exc}")
        yield p, browser
        browser.close()


@pytest.fixture
def server(tmp_path):
    settings = Settings(users={"abel@example.com": "Abel"}, dev_user="abel@example.com",
                        data_dir=str(tmp_path))
    app = create_app(settings)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield app, f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture
def page(browser, server):
    p, chromium = browser
    _, base_url = server
    context = chromium.new_context(**p.devices["Pixel 7"], base_url=base_url)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    # Network failures we cause on purpose (500s, 422s) also log console errors; ignore those.
    page.on("console", lambda msg: msg.type == "error"
            and not msg.text.startswith("Failed to load resource") and errors.append(msg.text))
    yield page
    context.close()
    assert errors == [], f"browser errors: {errors}"


def photo_file(name="gift.jpg"):
    out = io.BytesIO()
    Image.new("RGB", (400, 300), (30, 120, 200)).save(out, format="JPEG")
    return {"name": name, "mimeType": "image/jpeg", "buffer": out.getvalue()}


def take_photo(page, via="label.fab"):
    with page.expect_file_chooser() as chooser:
        page.locator(via).tap()
    chooser.value.set_files(photo_file())


def chip(page, label, scope="#add-dialog"):
    return page.locator(f"{scope} .chip").filter(has=page.get_by_text(label, exact=True))
