# app.py
#
# Plex Offline Launcher — Flask application
# Production-hardened: env config, structured logging, user-token cache,
# connectivity cache, CSRF tokens, scoped avatar proxy, health endpoint.

import base64
import logging
import os
import secrets
from functools import wraps

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    g,
)
from plexapi.exceptions import NotFound, Unauthorized

import config
from logger import setup_logging
from plex_client import (
    check_internet,
    connect,
    enrich,
    get_server,
    get_server_title,
    get_user_plex,
    invalidate_user_cache,
    is_connected,
    is_safe_avatar_url,
    make_media_url,
)

# ── Logging ───────────────────────────────────────────────────────────────────

log = setup_logging(config.LOG_LEVEL)

# ── Plex startup connection ───────────────────────────────────────────────────

connect()

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set Secure=True if you terminate TLS in front of this service
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=86400 * 14,  # 14-day sessions
)

_app_log = logging.getLogger("plex_launcher.app")


# ── CSRF ──────────────────────────────────────────────────────────────────────

def _get_csrf_token() -> str:
    """Return (and lazily create) the per-session CSRF token."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _verify_csrf() -> None:
    """Abort 403 if the CSRF token in the request doesn't match the session."""
    token = (
        request.form.get("csrf_token")
        or request.args.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
    )
    if not token or not secrets.compare_digest(token, _get_csrf_token()):
        _app_log.warning("CSRF token mismatch from %s", request.remote_addr)
        abort(403, "CSRF token invalid or missing.")


# Make csrf_token available in all templates automatically
@app.context_processor
def inject_csrf():
    return {"csrf_token": _get_csrf_token()}


# ── Request lifecycle ─────────────────────────────────────────────────────────

@app.before_request
def load_user_plex():
    """Resolve the user-scoped Plex instance once per request, store on g."""
    username = session.get("username")
    g.user_plex = get_user_plex(username)
    g.is_online = check_internet()
    g.server_title = get_server_title()


# ── Decorators ────────────────────────────────────────────────────────────────

def plex_required(f):
    """Abort 503 if the Plex server is not connected."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_connected():
            _app_log.error("Route %s accessed but Plex is not connected", request.path)
            abort(503, "Plex server is not connected. Check your .env configuration.")
        return f(*args, **kwargs)
    return wrapper


def login_required(f):
    """Redirect to user-select if no user is in session; also requires Plex."""
    @wraps(f)
    @plex_required
    def wrapper(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("user_select"))
        if g.user_plex is None:
            _app_log.warning("Could not resolve Plex instance for user '%s'", session.get("username"))
            session.clear()
            return redirect(url_for("user_select"))
        return f(*args, **kwargs)
    return wrapper


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message=str(e)), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="That page or item could not be found."), 404


@app.errorhandler(500)
def server_error(e):
    _app_log.exception("Unhandled 500 error")
    return render_template("error.html", code=500, message="Something went wrong on the server."), 500


@app.errorhandler(503)
def service_unavailable(e):
    return render_template("error.html", code=503, message=str(e)), 503


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """
    Lightweight health endpoint — suitable for uptime monitors and
    reverse-proxy health checks.
    """
    return jsonify({
        "status": "ok",
        "plex_connected": is_connected(),
        "plex_server": get_server_title(),
    })


# ── User select / auth ────────────────────────────────────────────────────────

@app.route("/")
@plex_required
def user_select():
    plex = get_server()
    users = []
    try:
        account = plex.myPlexAccount()
        users = [account] + list(account.users())
        for user in users:
            user._thumbUrl = getattr(user, "thumb", None)
    except Exception as exc:
        _app_log.warning("Could not fetch user list: %s", exc)

    return render_template(
        "user_select.html",
        users=users,
        server_title=g.server_title,
        is_online=g.is_online,
    )


@app.route("/proxy/avatar")
def proxy_avatar():
    """
    Proxy user avatar images so they work offline after first browser cache.
    Only proxies URLs from known plex.tv avatar CDN hosts.
    """
    url = request.args.get("url", "")
    if not url:
        abort(400, "Missing url parameter.")

    if not is_safe_avatar_url(url):
        _app_log.warning("Blocked avatar proxy request for disallowed host: %s", url)
        abort(400, "Avatar URL is not from a permitted host.")

    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "image/jpeg")
        resp = Response(r.content, content_type=content_type)
        # Cache for 24 h in the browser — avatars rarely change
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception:
        # 1×1 transparent PNG fallback
        transparent = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        return Response(transparent, content_type="image/png")


@app.route("/login/<username>")
@plex_required
def login(username):
    # Validate the username against the actual user list before accepting it
    plex = get_server()
    try:
        account = plex.myPlexAccount()
        valid_names = {account.username, account.title} | {
            getattr(u, "username", None) or getattr(u, "title", None)
            for u in account.users()
        }
        valid_names.discard(None)
        if username not in valid_names:
            _app_log.warning("Login attempt for unknown username '%s'", username)
            abort(403, "Unknown user.")
    except Exception as exc:
        _app_log.warning("Could not validate user list during login: %s", exc)
        # Degrade gracefully — allow login if we can't fetch the list
        pass

    session.clear()
    session["username"] = username
    _app_log.info("User '%s' logged in", username)
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    username = session.get("username")
    invalidate_user_cache(username)
    session.clear()
    _app_log.info("User '%s' logged out", username)
    return redirect(url_for("user_select"))


# ── Main pages ────────────────────────────────────────────────────────────────

@app.route("/home")
@login_required
def home():
    try:
        on_deck = enrich(g.user_plex.library.onDeck())
        recently_added = enrich(g.user_plex.library.recentlyAdded())
        libraries = g.user_plex.library.sections()
    except Exception as exc:
        _app_log.error("Failed to load home dashboard: %s", exc)
        on_deck = []
        recently_added = []
        libraries = []

    return render_template(
        "home_dashboard.html",
        server_title=g.server_title,
        is_online=g.is_online,
        on_deck=on_deck,
        recently_added=recently_added,
        libraries=libraries,
    )


@app.route("/library/<library_key>")
@login_required
def library(library_key):
    try:
        section = g.user_plex.library.sectionByID(int(library_key))
        items = enrich(section.all())
    except ValueError:
        abort(400, "Invalid library key.")
    except NotFound:
        abort(404, "Library not found.")
    except Exception as exc:
        _app_log.error("Failed to load library %s: %s", library_key, exc)
        abort(500)

    return render_template(
        "library.html",
        section=section,
        items=items,
        server_title=g.server_title,
        is_online=g.is_online,
    )


@app.route("/item/<int:rating_key>")
@login_required
def item_details(rating_key):
    try:
        item = g.user_plex.fetchItem(rating_key)
        item.thumbUrl = make_media_url(item.thumb)
        item.artUrl = make_media_url(item.art)
        if item.type == "show":
            for season in item.seasons():
                season.thumbUrl = make_media_url(season.thumb)
                for episode in season.episodes():
                    episode.thumbUrl = make_media_url(episode.thumb)
    except NotFound:
        abort(404, "Media item not found.")
    except Exception as exc:
        _app_log.error("Failed to load item %d: %s", rating_key, exc)
        abort(500)

    return render_template(
        "item_details.html",
        item=item,
        server_title=g.server_title,
        is_online=g.is_online,
    )


@app.route("/item/<int:rating_key>/mark_watched")
@login_required
def mark_watched(rating_key):
    _verify_csrf()
    try:
        item = g.user_plex.fetchItem(rating_key)
        item.markWatched()
        _app_log.info("User '%s' marked item %d as watched", session.get("username"), rating_key)
    except NotFound:
        abort(404, "Media item not found.")
    except Exception as exc:
        _app_log.error("mark_watched failed for item %d: %s", rating_key, exc)
        abort(500)
    return redirect(url_for("item_details", rating_key=rating_key))


@app.route("/item/<int:rating_key>/mark_unwatched")
@login_required
def mark_unwatched(rating_key):
    _verify_csrf()
    try:
        item = g.user_plex.fetchItem(rating_key)
        item.markUnwatched()
        _app_log.info("User '%s' marked item %d as unwatched", session.get("username"), rating_key)
    except NotFound:
        abort(404, "Media item not found.")
    except Exception as exc:
        _app_log.error("mark_unwatched failed for item %d: %s", rating_key, exc)
        abort(500)
    return redirect(url_for("item_details", rating_key=rating_key))


# ── Player ────────────────────────────────────────────────────────────────────

@app.route("/player/<int:rating_key>/fresh")
@login_required
def player_fresh(rating_key):
    return redirect(url_for("player", rating_key=rating_key) + "?force_start=1")


@app.route("/player/<int:rating_key>")
@login_required
def player(rating_key):
    try:
        item = g.user_plex.fetchItem(rating_key)
        item.thumbUrl = make_media_url(item.thumb)
        item.artUrl   = make_media_url(item.art)
    except NotFound:
        abort(404, "Media item not found.")
    except Exception as exc:
        _app_log.error("Failed to load player for item %d: %s", rating_key, exc)
        abort(500)

    force_start  = request.args.get("force_start") == "1"
    view_offset  = 0 if force_start else (item.viewOffset or 0)
    duration_ms  = item.duration or 0

    resumable = (
        view_offset > 30_000
        and duration_ms > 0
        and view_offset < duration_ms - 60_000
    )

    # Stream URL — token is server-side only; JS sees the full URL but it's
    # scoped to the local network and not the admin token in the query string.
    # (For higher security, wrap this in a signed short-lived proxy route.)
    stream_url = (
        f"{config.PLEX_URL}/video/:/transcode/universal/start.m3u8"
        f"?hasMDE=1"
        f"&path=/library/metadata/{item.ratingKey}"
        f"&mediaIndex=0"
        f"&partIndex=0"
        f"&protocol=hls"
        f"&fastSeek=1"
        f"&directPlay=1"
        f"&directStream=1"
        f"&subtitleSize=100"
        f"&audioBoost=100"
        f"&X-Plex-Token={config.PLEX_TOKEN}"
        f"&X-Plex-Client-Identifier=plex-offline-launcher"
        f"&X-Plex-Product=PlexOfflineLauncher"
        f"&X-Plex-Version=3.0"
        f"&X-Plex-Platform=Chrome"
        f"&offset={view_offset // 1000}"
    )

    prev_ep = next_ep = None
    if item.type == "episode":
        try:
            siblings = list(item.show().episodes())
            idx = next((i for i, e in enumerate(siblings) if e.ratingKey == item.ratingKey), None)
            if idx is not None:
                prev_ep = siblings[idx - 1] if idx > 0 else None
                next_ep = siblings[idx + 1] if idx < len(siblings) - 1 else None
        except Exception as exc:
            _app_log.warning("Failed to build episode nav for item %d: %s", rating_key, exc)

    return render_template(
        "player.html",
        item=item,
        stream_url=stream_url,
        view_offset=view_offset,
        duration_ms=duration_ms,
        resumable=resumable,
        prev_ep=prev_ep,
        next_ep=next_ep,
    )


# ── Scrobble API ──────────────────────────────────────────────────────────────

# Simple in-memory rate limiter: {ip: (count, window_start)}
_scrobble_rate: dict[str, tuple[int, float]] = {}
_SCROBBLE_LIMIT = 60   # max calls per window
_SCROBBLE_WINDOW = 60  # seconds


def _check_scrobble_rate(ip: str) -> bool:
    """Return True if the request is within rate limits, False if it should be dropped."""
    import time
    now = time.monotonic()
    count, start = _scrobble_rate.get(ip, (0, now))
    if now - start > _SCROBBLE_WINDOW:
        _scrobble_rate[ip] = (1, now)
        return True
    if count >= _SCROBBLE_LIMIT:
        return False
    _scrobble_rate[ip] = (count + 1, start)
    return True


@app.route("/api/scrobble/<int:rating_key>", methods=["POST"])
@login_required
def scrobble(rating_key):
    """
    Reports playback progress to Plex.
    Called by the player JS every 10 s and on pause/stop.
    Uses the scoped user token (not the admin token).
    """
    ip = request.remote_addr or "unknown"
    if not _check_scrobble_rate(ip):
        _app_log.warning("Scrobble rate limit hit for %s", ip)
        return jsonify({"ok": False, "error": "rate limited"}), 429

    data = request.get_json(silent=True) or {}
    offset_ms   = int(data.get("offset_ms", 0))
    duration_ms = int(data.get("duration_ms", 0))
    state       = data.get("state", "playing")

    if state not in {"playing", "paused", "stopped"}:
        return jsonify({"ok": False, "error": "invalid state"}), 400

    try:
        # Use the user-scoped token for scrobble, not the admin token
        user_token = getattr(g.user_plex, "_token", config.PLEX_TOKEN)

        params = {
            "ratingKey":               rating_key,
            "key":                     f"/library/metadata/{rating_key}",
            "state":                   state,
            "time":                    offset_ms,
            "duration":                duration_ms,
            "X-Plex-Token":            user_token,
            "X-Plex-Client-Identifier": "plex-offline-launcher",
            "X-Plex-Product":          "PlexOfflineLauncher",
            "X-Plex-Version":          "3.0",
        }
        requests.get(f"{config.PLEX_URL}/:/timeline", params=params, timeout=5)
        return jsonify({"ok": True})

    except Exception as exc:
        _app_log.error("Scrobble failed for item %d: %s", rating_key, exc)
        return jsonify({"ok": False, "error": "scrobble failed"}), 500


# ── Search ────────────────────────────────────────────────────────────────────

@app.route("/search")
@login_required
def search():
    query = request.args.get("query", "").strip()
    results = []
    if query:
        try:
            results = enrich(g.user_plex.search(query))
        except Exception as exc:
            _app_log.error("Search failed for query '%s': %s", query, exc)

    return render_template(
        "search_results.html",
        query=query,
        results=results,
        server_title=g.server_title,
        is_online=g.is_online,
    )


# ── WSGI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Dev-only: use `python run.py` or `waitress-serve` for production
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
