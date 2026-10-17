# Lump of Coal

A self-hosted gift tracker for a household: FastAPI + Jinja + plain JavaScript,
SQLite through SQLAlchemy, Alembic migrations, shipped as a Docker image. The
README is written for people running it; this file is for working on it.

## Commands

```
.venv/bin/python -m pytest -q                      # everything, ~70s
.venv/bin/python -m pytest -q tests/e2e            # browser tests (Playwright)
.venv/bin/python -m playwright install chromium    # if e2e tests are skipped
.venv/bin/uvicorn app.main:create_app --factory --env-file .env --reload
```

CI (`.github/workflows/image.yml`) runs the tests on every push and PR. On
master it also builds `ghcr.io/ogdengamer/lumpofcoal:latest` (amd64 + arm64);
a `vX.Y.Z` tag builds `:X.Y.Z`, `:X.Y` and `:X`. `compose.yaml` follows `:1`.

## Rules that matter

- **Nobody sees gifts meant for them.** Every read or write of gifts and photos
  goes through `GiftRepo` (`app/visibility.py`), which builds the rule into the
  query. Never query `Gift` or `Photo` directly from routes or helpers. Budgets
  aimed at the viewer are hidden the same way (`app/manage.py`, `app/budget.py`).
- **Migrations must not lose data.** They run on start (`app/db.py`). SQLite
  can't alter tables, so batch mode rebuilds them; `app/migrations/env.py`
  switches foreign keys off while migrating, or rebuilding `gifts` would
  cascade-delete its photo links (this happened in 1.1 testing). A migration
  that rebuilds a table gets an upgrade test with real rows in related tables
  (see `test_upgrade_keeps_gifts_their_photos_and_allows_ordered`).
  `tests/test_models.py` fails if models and migrations disagree.
- **Startup deletes unattached photos** older than a day
  (`delete_orphan_photos`), so a bug that drops `gift_photos` rows also deletes
  the files.
- **Test data is made up.** `tests/family.py` seeds a fictional household
  (Abel and Jules, partners; children Emma, Elsa, Shay, Astrid; and so on). Never
  put real names, gifts or photos in tests, fixtures, commits or PRs.
- Relations have a user-set order (`Relation.position`); read them with
  `manage.relations(session)` so the order is the same everywhere.
- Gift statuses: idea, ordered, bought (labelled "Have it"), wrapped, given.
  Everything except idea counts as spent. Labels come from `STATUS_LABELS`.

## Layout

- `app/routes/`: one module per area (gallery, gifts, budget, settings,
  account, photos, welcome, pwa). Settings tabs are listed in `TABS` in
  `app/routes/settings.py`, one template each (`settings_<tab>.html`).
- `app/static/add.js`: all the JavaScript (gift form, chips, settings autosave,
  confirms). `app/static/app.css`: all the styles, colours as tokens with a dark
  theme.
- `app/backup.py`: the optional backup sidecar (compose profile `backup`). It
  talks to the app through files in `data/` (heartbeat, request, status).
- `app/auth.py`, `app/logins.py`: `AUTH_MODE=cloudflare` (Cloudflare Access
  JWT) or `password` (the app's own sign-in, invite links, no email).

## Working on it

- Match the surrounding code: short docstrings that explain why, plain words in
  the UI, no new dependencies or frameworks without asking.
- One change per branch, off an up-to-date master, with its own PR. Check the
  previous PR is merged before starting the next.
- New behaviour gets tests: unit tests for rules and routes, an e2e test when
  JavaScript is involved. Check that a regression test fails without the fix.
- Releasing: merged changes are tried out on `:latest` first. Then tag
  `vX.Y.Z` on master and write a GitHub release (what changed, any `.env`
  changes, and "tap Back up now first" when there's a migration). Releases are
  the changelog; there's no CHANGELOG file.
