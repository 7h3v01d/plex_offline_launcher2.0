# plex_client.py
#
# Manages the Plex server connection lifecycle and provides:
#   - Startup connection with retry
#   - Per-user token cache (avoids a switchUser() API call on every request)
#   - Connectivity check cache (avoids an external HTTP call on every page load)

import logging
import threading
import time
from urllib.parse import urlparse

import requests
from plexapi.server import PlexServer

import config

log = logging.getLogger("plex_launcher.client")

# ── Globals (module-level singletons) ─────────────────────────────────────────

_plex: PlexServer | None = None
_server_title: str = "Plex Server"
_connect_error: str | None = None

# User token cache: {username: (token, expiry_timestamp)}
_user_cache: dict[str, tuple[str, float]] = {}
_user_cache_lock = threading.Lock()

# Connectivity cache: (result: bool, expiry_timestamp)
_connectivity_cache: tuple[bool, float] = (False, 0.0)
_connectivity_lock = threading.Lock()


# ── Connection ────────────────────────────────────────────────────────────────

def connect(retries: int = 2, retry_delay: float = 2.0) -> None:
    """
    Attempt to connect to the Plex server, storing the result in module globals.
    Call once at application startup. Safe to call again to reconnect.
    """
    global _plex, _server_title, _connect_error

    for attempt in range(1, retries + 2):
        try:
            log.info("Connecting to Plex at %s (attempt %d)…", config.PLEX_URL, attempt)
            server = PlexServer(config.PLEX_URL, config.PLEX_TOKEN, timeout=config.PLEX_CONNECT_TIMEOUT)
            _plex = server
            _server_title = server.friendlyName
            _connect_error = None
            log.info("✅  Connected to Plex server: '%s'", _server_title)
            return
        except Exception as exc:
            log.warning("Plex connection attempt %d failed: %s", attempt, exc)
            _connect_error = str(exc)
            if attempt <= retries:
                time.sleep(retry_delay)

    log.error("❌  Could not connect to Plex after %d attempts. Last error: %s", retries + 1, _connect_error)
    _plex = None
    _server_title = "Plex Server (Connection Failed)"


def get_server() -> PlexServer | None:
    return _plex


def get_server_title() -> str:
    return _server_title


def is_connected() -> bool:
    return _plex is not None


# ── Per-user Plex instance ────────────────────────────────────────────────────

def get_user_plex(username: str | None) -> PlexServer | None:
    """
    Return a Plex instance scoped to the given managed user.
    Results are cached for USER_CACHE_TTL seconds to avoid repeated API calls.
    Falls back to the admin instance if switchUser fails.
    """
    if _plex is None:
        return None

    if not username:
        return _plex

    now = time.monotonic()

    with _user_cache_lock:
        cached = _user_cache.get(username)
        if cached:
            token, expiry = cached
            if now < expiry:
                # Reconstruct a scoped PlexServer from the cached token
                try:
                    return PlexServer(config.PLEX_URL, token, timeout=config.PLEX_CONNECT_TIMEOUT)
                except Exception:
                    pass  # Cache invalid — fall through to fresh lookup

    # Cache miss or stale — do the switchUser call
    try:
        scoped = _plex.switchUser(username)
        token = scoped._token
        with _user_cache_lock:
            _user_cache[username] = (token, now + config.USER_CACHE_TTL)
        log.debug("Cached user token for '%s' (TTL %ds)", username, config.USER_CACHE_TTL)
        return scoped
    except Exception as exc:
        log.warning("switchUser('%s') failed (%s) — falling back to admin context", username, exc)
        return _plex


def invalidate_user_cache(username: str | None = None) -> None:
    """Evict one or all users from the token cache."""
    with _user_cache_lock:
        if username:
            _user_cache.pop(username, None)
        else:
            _user_cache.clear()


# ── Internet connectivity ─────────────────────────────────────────────────────

def check_internet() -> bool:
    """
    Returns True if an internet connection appears to be available.
    Result is cached for CONNECTIVITY_CACHE_TTL seconds to avoid
    adding latency to every page render.
    """
    global _connectivity_cache

    now = time.monotonic()
    result, expiry = _connectivity_cache

    if now < expiry:
        return result

    with _connectivity_lock:
        # Re-read under lock in case another thread already refreshed
        result, expiry = _connectivity_cache
        if now < expiry:
            return result

        try:
            requests.get(
                "http://detectportal.firefox.com/success.txt",
                timeout=3,
                allow_redirects=False,
            )
            fresh = True
        except (requests.ConnectionError, requests.Timeout):
            fresh = False

        _connectivity_cache = (fresh, now + config.CONNECTIVITY_CACHE_TTL)
        log.debug("Connectivity check: %s (cached for %ds)", "online" if fresh else "offline", config.CONNECTIVITY_CACHE_TTL)
        return fresh


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_media_url(path: str | None) -> str | None:
    """Append the Plex token to a relative media path."""
    if not path:
        return None
    return f"{config.PLEX_URL}{path}?X-Plex-Token={config.PLEX_TOKEN}"


def enrich(items: list) -> list:
    """Attach thumbUrl and ensure viewOffset/duration exist on each item."""
    for item in items:
        item.thumbUrl = make_media_url(item.thumb)
        if not hasattr(item, "viewOffset") or item.viewOffset is None:
            item.viewOffset = 0
        if not hasattr(item, "duration") or item.duration is None:
            item.duration = 0
    return items


def is_safe_avatar_url(url: str) -> bool:
    """Validate that an avatar proxy URL points to a known safe host."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return any(
            host == allowed or host.endswith("." + allowed)
            for allowed in config.ALLOWED_AVATAR_HOSTS
        )
    except Exception:
        return False
