"""Settings, read from the environment (see .env.example)."""

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _parse_users(raw: str) -> dict[str, str]:
    """Parse APP_USERS: "email=Person Name,email=Person Name"."""
    users: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        email, sep, person = entry.partition("=")
        if not sep or not email.strip() or not person.strip():
            raise ConfigError(f"APP_USERS entry must be email=Person: {entry!r}")
        users[email.strip().lower()] = person.strip()
    return users


AUTH_MODES = ("cloudflare", "password")


@dataclass(frozen=True)
class Settings:
    # "cloudflare": identity from Cloudflare Access. "password": the app's own sign-in.
    auth_mode: str = "cloudflare"
    # Cloudflare Access, e.g. team_domain="yourteam.cloudflareaccess.com"
    access_team_domain: str = ""
    access_aud: str = ""
    # First logins for a brand-new database only (see logins.bootstrap_users):
    # email -> the Person's name.
    users: dict[str, str] = field(default_factory=dict)
    # Local development only: skip Access and act as this email.
    dev_user: str = ""
    data_dir: str = "/data"

    @property
    def access_certs_url(self) -> str:
        return f"https://{self.access_team_domain}/cdn-cgi/access/certs"

    @property
    def access_issuer(self) -> str:
        return f"https://{self.access_team_domain}"

    def validate(self) -> None:
        if self.auth_mode not in AUTH_MODES:
            raise ConfigError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}")
        if self.dev_user:
            # Refuse to run with the bypass in a configured production setup.
            if self.access_aud:
                raise ConfigError("DEV_USER must not be set alongside CF_ACCESS_AUD")
        elif self.auth_mode == "cloudflare" and not (self.access_team_domain and self.access_aud):
            raise ConfigError("CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are required "
                              "(or set AUTH_MODE=password)")


def load_settings() -> Settings:
    env = os.environ
    settings = Settings(
        auth_mode=env.get("AUTH_MODE", "cloudflare").strip().lower() or "cloudflare",
        access_team_domain=env.get("CF_ACCESS_TEAM_DOMAIN", "").strip(),
        access_aud=env.get("CF_ACCESS_AUD", "").strip(),
        users=_parse_users(env.get("APP_USERS", "")),
        dev_user=env.get("DEV_USER", "").strip().lower(),
        data_dir=env.get("DATA_DIR", "/data"),
    )
    settings.validate()
    return settings
