# Lump of Coal

A small self-hosted gift tracker for a household. Snap a photo of a gift, pick who
it's for, and it's saved. Set budgets ("$100 per child for Christmas") and see
what's left. **Nobody ever sees the gifts meant for them**, so you can share it
with your partner without spoiling surprises.

- Built for phones: install it to your home screen and add a gift in a few taps.
  On Android you can also share a photo to it from another app.
- Recipients can be people or groups ("the grandkids"), for any occasion
  (Christmas, birthdays, or your own).
- Budgets by relation, person or group, with spending tracked against them.
- Gift ideas through to bought, wrapped and given.
- Your data stays on your machine: one SQLite database plus a photos folder, with
  optional nightly backups.

This is a personal project, shared as-is in case it's useful. Issues and
suggestions are welcome, but there's no promise of support.

## Running it

You need a machine with Docker. The app runs as user ID 1000 inside the container.

```
mkdir lumpofcoal && cd lumpofcoal
curl -fsSLO https://raw.githubusercontent.com/ogdengamer/lumpofcoal/master/compose.yaml
curl -fsSL https://raw.githubusercontent.com/ogdengamer/lumpofcoal/master/.env.example -o .env
mkdir data && sudo chown 1000:1000 data
nano .env              # at least set TZ
docker compose up -d
```

Open `http://<this machine>:8000`. The first visit asks you to create your login.
A short **Getting started** checklist then walks you through adding people,
budgets and backups. Settings links back to it.

To reach it from your phone away from home, or to install it as an app, it needs
HTTPS. See [HTTPS](#https).

### Updating

```
docker compose pull && docker compose up -d
```

That gets the newest 1.x release; the
[release notes](https://github.com/ogdengamer/lumpofcoal/releases) say what changed
and whether `.env` needs anything new. The database upgrades itself on start.
Upgrades can't be undone by going back to an older version, so it's worth tapping
**Back up now** (Settings → Backups) first.

`LUMPOFCOAL_VERSION` in `.env` picks what you run:

| Value | You get |
|---|---|
| `1` (the default) | every 1.x release |
| `1.1.0` | exactly that release, until you change it |
| `latest` | every change as soon as it's merged, before it's been tried out |

A 2.0 would mean something needs your attention, so `1` never moves to it on its
own.

## HTTPS

Phones only install web apps, and browsers only keep you signed in safely, over
HTTPS. Pick whichever fits:

**Tailscale** (easiest if your household already uses it, or will). The app stays
private to your tailnet, and every phone needs the Tailscale app. With HTTPS
certificates turned on in the Tailscale admin console:

```
sudo tailscale serve --bg 8000
```

It's then at `https://<machine>.<tailnet>.ts.net`.

**Caddy** (a public address, with free certificates). You need a domain name
pointing at this machine and ports 80 and 443 reachable from the internet. Make a
`Caddyfile`:

```
gifts.example.com {
    reverse_proxy lumpofcoal:8000
}
```

and a `compose.override.yaml` next to `compose.yaml` (Compose reads it
automatically):

```yaml
services:
  lumpofcoal:
    ports: !reset []   # only Caddy needs to reach it
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy:/data
```

**Cloudflare Tunnel and Access.** If you already use Cloudflare, put the app behind
a tunnel and an Access application, and set `AUTH_MODE=cloudflare` (below).

## Signing in and app users

`AUTH_MODE` in `.env` picks how people sign in:

- `password`: the app's own sign-in, with email and password. People get a one-time
  sign-in link (good for 7 days) to choose their password; the app shows the link
  and you send it however you like. Nothing is emailed. A forgotten password is
  reset the same way: someone else makes them a new link.
- `cloudflare`: through Cloudflare Access (set `CF_ACCESS_TEAM_DOMAIN` and
  `CF_ACCESS_AUD`). Giving someone a login in the app isn't enough; also add their
  email to the Access policy.

Every app user can do everything; there are no admins. Each sees every gift
except gifts for them (or for a group they're in), and every budget except their
own.

App users are managed in **Settings → App users**. If nobody can sign in, make a
sign-in link from the server:

```
docker compose exec lumpofcoal python -m app.users invite "Name" --email name@example.com
docker compose exec lumpofcoal python -m app.users list
```

A new copy starts with the usual relations (Partner, Child, …) and occasions
(Christmas, Birthday, Easter, Just Because); change them in Settings.

## Backups

The optional `backup` service writes `gifts-backup-YYYYMMDD_HHMMSS.tar.gz` (the
database and all photos) every night, and deletes old ones. Settings in `.env`:

| Setting | Default | |
|---|---|---|
| `COMPOSE_PROFILES=backup` | off | turns the service on |
| `BACKUP_DIR` | `./backups` | folder on this machine the backups go to |
| `BACKUP_AT` | `00:30` | time of day, in `TZ` |
| `BACKUP_KEEP_DAYS` | `30` | how long to keep each one |

Use another disk or a network share for `BACKUP_DIR` if you can: a backup on the
same disk as the app won't survive that disk failing.

The service only writes if that folder holds a `.lumpofcoal-backups` marker file.
If a network share isn't mounted, the folder underneath is empty, so the backup
fails loudly instead of quietly filling the local disk. To set up:

```
# 1. Make the folder (or mount your share there) and let the container write to it
mkdir -p backups && sudo chown 1000:1000 backups
# 2. Create the marker as the container's user (this also proves it can write)
docker compose run --rm backup touch /backups/.lumpofcoal-backups
# 3. Add COMPOSE_PROFILES=backup to .env, then start it
docker compose up -d
```

Then open **Settings → Backups** and tap **Back up now** to check it works. That
page shows the last backup or why it failed; `docker logs lumpofcoal-backup` has
the details (failures say `BACKUP FAILED`).

For an NFS share, mount it on the host (not in the container) with `nofail`, so a
NAS that's down doesn't stop the machine booting. For example, in `/etc/fstab`:

```
nas.local:/volume1/backups/gifts  /mnt/gifts-backups  nfs  nfsvers=3,rw,_netdev,nofail  0  0
```

and set `BACKUP_DIR=/mnt/gifts-backups`.

**Download a backup**, also on Settings → Backups, makes a fresh copy for anyone
signed in, even without the backup service. It holds every gift, including ones
for whoever downloads it, so the button warns first.

To restore (the archive holds `gifts.db` and `photos/`):

```
docker compose stop
mkdir -p data/before-restore && mv data/gifts.db* data/before-restore/
tar xzf backups/gifts-backup-YYYYMMDD_HHMMSS.tar.gz -C data
docker compose start
```

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium   # for the browser tests
cp .env.example .env   # set APP_USERS and DEV_USER (skips sign-in), TZ
.venv/bin/uvicorn app.main:create_app --factory --env-file .env --reload
.venv/bin/python -m pytest
```

FastAPI, Jinja templates and plain JavaScript; SQLite through SQLAlchemy, with
Alembic migrations that run on start. All access to gifts goes through
`app/visibility.py`, which is what keeps people's own gifts hidden from them.

To run your own build in Docker, add to `compose.override.yaml`:

```yaml
services:
  lumpofcoal: {build: .}
  backup: {build: .}
```

and use `docker compose up -d --build`.

## License

MIT. See [LICENSE](LICENSE).
