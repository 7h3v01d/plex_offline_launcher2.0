# config.py
#
# Loads all settings from environment variables (via a .env file).
# Raises clear errors at startup if required values are missing or look unsafe.

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in your values."
        )
    return val


def _optional(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


# ── Required ──────────────────────────────────────────────────────────────────

PLEX_URL   = _require("PLEX_URL").rstrip("/")
PLEX_TOKEN = _require("PLEX_TOKEN")
SECRET_KEY = _require("SECRET_KEY")

# Guard against the placeholder value being used as-is
_UNSAFE_KEYS = {
    "change_me_to_a_long_random_string",
    "your_plex_token_here",
    "a-really-secret-and-random-string-that-you-make-up",
}
if SECRET_KEY in _UNSAFE_KEYS:
    raise EnvironmentError(
        "SECRET_KEY is still set to a placeholder value. "
        "Generate a real key with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
if len(SECRET_KEY) < 24:
    raise EnvironmentError("SECRET_KEY must be at least 24 characters long.")

# ── Optional with sensible defaults ───────────────────────────────────────────

PLEX_CONNECT_TIMEOUT  = int(_optional("PLEX_CONNECT_TIMEOUT", "10"))
USER_CACHE_TTL        = int(_optional("USER_CACHE_TTL", "300"))
CONNECTIVITY_CACHE_TTL = int(_optional("CONNECTIVITY_CACHE_TTL", "30"))
PORT                  = int(_optional("PORT", "5000"))
LOG_LEVEL             = _optional("LOG_LEVEL", "INFO").upper()

# ── Derived ───────────────────────────────────────────────────────────────────

# Allowed avatar proxy origins — only permit the known plex.tv avatar CDN
ALLOWED_AVATAR_HOSTS = {
    "plex.direct",
    "plex.tv",
    "assets.plex.tv",
    "secure.gravatar.com",
}
