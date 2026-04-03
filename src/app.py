# app.py

import time
import threading
import requests
import base64
from flask import Flask, render_template, abort, request, session, redirect, url_for, jsonify, Response
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound
from functools import wraps
import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

try:
    print("Connecting to Plex server as admin...")
    plex = PlexServer(config.PLEX_URL, config.PLEX_TOKEN, timeout=10)
    server_title = plex.friendlyName
    print(f"✅ Connection to '{server_title}' successful!")
except Exception as e:
    plex = None
    server_title = "Plex Server (Connection Failed)"
    print(f"❌ Could not connect to Plex. Error: {e}")

# ---------------------------------------------------------------------------
# Internet status cache
# Checked once every 15 seconds in a background thread.
# All page renders read from this cache — zero blocking network I/O per request.
# ---------------------------------------------------------------------------

_internet_status = {'online': False, 'checked_at': 0}
_internet_lock   = threading.Lock()

def _refresh_internet_status():
    """Background thread: polls internet connectivity every 15 seconds."""
    while True:
        try:
            requests.get("http://detectportal.firefox.com/success.txt", timeout=3)
            online = True
        except Exception:
            online = False
        with _internet_lock:
            _internet_status['online']     = online
            _internet_status['checked_at'] = time.monotonic()
        time.sleep(15)

_status_thread = threading.Thread(target=_refresh_internet_status, daemon=True)
_status_thread.start()

def get_internet_status():
    with _internet_lock:
        return _internet_status['online']

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def add_auth_to_url(url):
    if not url:
        return None
    return f"{config.PLEX_URL}{url}?X-Plex-Token={config.PLEX_TOKEN}"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not plex:
            abort(500, "Plex server not connected.")
        user_plex = get_plex_instance()
        if not user_plex:
            return redirect(url_for('user_select'))
        return f(*args, **kwargs, user_plex=user_plex)
    return decorated

def get_plex_instance():
    username = session.get('username')
    if not username:
        return None
    try:
        return plex.switchUser(username)
    except Exception:
        return plex

def enrich(items):
    for item in items:
        item.thumbUrl = add_auth_to_url(item.thumb)
        if not hasattr(item, 'viewOffset') or item.viewOffset is None:
            item.viewOffset = 0
        if not hasattr(item, 'duration') or item.duration is None:
            item.duration = 0
    return items

def proxy_image(url):
    """Fetch an external image and proxy it locally (for offline avatar access)."""
    try:
        r = requests.get(url, timeout=5)
        return Response(r.content, content_type=r.headers.get('Content-Type', 'image/jpeg'))
    except Exception:
        transparent_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
        )
        return Response(transparent_png, content_type='image/png')

def get_streams(item):
    """Return audio and subtitle streams from the first media part of an item."""
    try:
        part  = item.media[0].parts[0]
        audio = []
        subs  = []
        for s in part.streams:
            label = s.extendedDisplayTitle or s.displayTitle or s.language or s.codec or f"Track {s.index}"
            if s.streamType == 2:    # audio
                audio.append({
                    'id':       s.id,
                    'index':    s.index,
                    'label':    label,
                    'selected': bool(s.selected),
                    'codec':    s.codec or '',
                    'channels': getattr(s, 'channels', None),
                })
            elif s.streamType == 3:  # subtitle
                subs.append({
                    'id':       s.id,
                    'index':    s.index,
                    'label':    label,
                    'selected': bool(s.selected),
                    'forced':   bool(getattr(s, 'forced', False)),
                    'codec':    s.codec or '',
                })
        return {'audio_streams': audio, 'subtitle_streams': subs, 'part_id': part.id}
    except Exception:
        return {'audio_streams': [], 'subtitle_streams': [], 'part_id': None}

def safe_total_size(section):
    """Return section.totalSize with a fallback for older plexapi versions."""
    try:
        return section.totalSize
    except AttributeError:
        pass
    try:
        # Older plexapi: do a minimal search and read the X-Plex-Container-Total-Size header
        results = section.search(container_start=0, container_size=1, maxresults=1)
        # plexapi stores the last response total on the server object
        return getattr(section, '_total', None)
    except Exception:
        return None   # unknown — JS will paginate until it gets an empty page

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html',
                           code=404,
                           title="Page Not Found",
                           message=str(e),
                           server_title=server_title,
                           is_online=get_internet_status()), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('error.html',
                           code=500,
                           title="Server Error",
                           message=str(e),
                           server_title=server_title,
                           is_online=get_internet_status()), 500

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def user_select():
    if not plex:
        return render_template('error.html',
                               code=503,
                               title="Cannot Connect to Plex",
                               message=(
                                   "The launcher could not connect to your Plex server. "
                                   f"Check that PLEX_URL ({config.PLEX_URL}) and PLEX_TOKEN "
                                   "are correct in config.py, and that the server is running."
                               ),
                               server_title="Plex Offline Launcher",
                               is_online=get_internet_status()), 503
    try:
        account = plex.myPlexAccount()
        users   = [account] + list(account.users())
        for user in users:
            user._thumbUrl = user.thumb
    except Exception:
        users = []
    return render_template('user_select.html',
                           users=users,
                           server_title=server_title,
                           is_online=get_internet_status())

@app.route('/proxy/avatar')
def proxy_avatar():
    url = request.args.get('url', '')
    if not url:
        abort(400)
    return proxy_image(url)

@app.route('/api/status')
def api_status():
    """Lightweight endpoint polled by the client-side status indicator."""
    return jsonify({'online': get_internet_status()})

@app.route('/login/<username>')
def login(username):
    session['username'] = username
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('user_select'))

@app.route('/home')
@login_required
def home(user_plex):
    on_deck        = enrich(user_plex.library.onDeck())
    recently_added = enrich(user_plex.library.recentlyAdded())
    libraries      = user_plex.library.sections()
    return render_template('home_dashboard.html',
                           server_title=server_title,
                           is_online=get_internet_status(),
                           on_deck=on_deck,
                           recently_added=recently_added,
                           libraries=libraries)

# ---------------------------------------------------------------------------
# Library — paginated
# ---------------------------------------------------------------------------
PAGE_SIZE = 48

@app.route('/library/<library_key>')
@login_required
def library(user_plex, library_key):
    try:
        section   = user_plex.library.sectionByID(int(library_key))
        sort      = request.args.get('sort', 'titleSort:asc')
        unwatched = request.args.get('unwatched', '0') == '1'

        # Fix 2: safe totalSize — doesn't crash on older plexapi
        total = safe_total_size(section)

        kwargs = dict(sort=sort, container_start=0, container_size=PAGE_SIZE, maxresults=PAGE_SIZE)
        if unwatched:
            kwargs['unwatched'] = True

        items = enrich(section.search(**kwargs))

        return render_template('library.html',
                               section=section,
                               items=items,
                               # JS uses numeric total to decide when pagination is exhausted;
                               # use a large sentinel when unknown so it keeps loading pages
                               total=total if total is not None else 999999,
                               # display_total is the human-readable count in the toolbar
                               display_total=total if total is not None else '?',
                               page_size=PAGE_SIZE,
                               sort=sort,
                               unwatched=unwatched,
                               server_title=server_title,
                               is_online=get_internet_status())
    except Exception as e:
        abort(404, f"Library not found: {e}")

@app.route('/api/library/<library_key>/page')
@login_required
def library_page(user_plex, library_key):
    """JSON endpoint for infinite scroll — returns the next page of cards."""
    try:
        section   = user_plex.library.sectionByID(int(library_key))
        offset    = int(request.args.get('offset', 0))
        sort      = request.args.get('sort', 'titleSort:asc')
        unwatched = request.args.get('unwatched', '0') == '1'

        kwargs = dict(sort=sort, container_start=offset, container_size=PAGE_SIZE, maxresults=PAGE_SIZE)
        if unwatched:
            kwargs['unwatched'] = True

        items = enrich(section.search(**kwargs))

        cards = []
        for item in items:
            vo  = item.viewOffset or 0
            dur = item.duration   or 0
            pct = int(vo / dur * 100) if dur > 0 else 0
            cards.append({
                'ratingKey': item.ratingKey,
                'title':     item.title,
                'year':      item.year,
                'thumbUrl':  item.thumbUrl,
                'isWatched': item.isWatched,
                'pct':       pct,
            })
        return jsonify({'cards': cards, 'offset': offset, 'count': len(cards)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/item/<int:rating_key>')
@login_required
def item_details(user_plex, rating_key):
    try:
        item = user_plex.fetchItem(rating_key)
        item.thumbUrl = add_auth_to_url(item.thumb)
        item.artUrl   = add_auth_to_url(item.art)
        if item.type == 'show':
            for season in item.seasons():
                season.thumbUrl = add_auth_to_url(season.thumb)
                for episode in season.episodes():
                    episode.thumbUrl = add_auth_to_url(episode.thumb)
        return render_template('item_details.html',
                               item=item,
                               server_title=server_title,
                               is_online=get_internet_status())
    except NotFound:
        abort(404, "Media not found.")

@app.route('/item/<int:rating_key>/mark_watched')
@login_required
def mark_watched(user_plex, rating_key):
    item = user_plex.fetchItem(rating_key)
    item.markWatched()
    return redirect(url_for('item_details', rating_key=rating_key))

@app.route('/item/<int:rating_key>/mark_unwatched')
@login_required
def mark_unwatched(user_plex, rating_key):
    item = user_plex.fetchItem(rating_key)
    item.markUnwatched()
    return redirect(url_for('item_details', rating_key=rating_key))

# Fix 5: player_fresh no longer fetches the item (it was dead code — the
# fetch result was thrown away before the redirect).
@app.route('/player/<int:rating_key>/fresh')
@login_required
def player_fresh(user_plex, rating_key):
    return redirect(url_for('player', rating_key=rating_key) + '?force_start=1')

@app.route('/player/<int:rating_key>')
@login_required
def player(user_plex, rating_key):
    try:
        item = user_plex.fetchItem(rating_key)
        item.thumbUrl = add_auth_to_url(item.thumb)
        item.artUrl   = add_auth_to_url(item.art)

        force_start = request.args.get('force_start') == '1'
        view_offset = 0 if force_start else (item.viewOffset or 0)
        duration_ms = item.duration or 0

        resumable = (
            view_offset > 30_000 and
            duration_ms > 0 and
            view_offset < duration_ms - 60_000
        )

        streams            = get_streams(item)
        selected_audio     = next((s for s in streams['audio_streams']    if s['selected']), None)
        selected_subtitle  = next((s for s in streams['subtitle_streams'] if s['selected']), None)
        audio_stream_id    = selected_audio['id']    if selected_audio    else None
        subtitle_stream_id = selected_subtitle['id'] if selected_subtitle else None

        stream_url = _build_stream_url(
            item.ratingKey, view_offset,
            audio_stream_id=audio_stream_id,
            subtitle_stream_id=subtitle_stream_id,
        )

        # Fix 3: derive show_rating_key safely for the back button
        # grandparentRatingKey is reliably set on fully-loaded episode objects.
        show_rating_key = None
        if item.type == 'episode':
            show_rating_key = getattr(item, 'grandparentRatingKey', None)
            if show_rating_key is None:
                # Fallback: navigate to the show via the show() call
                try:
                    show_rating_key = item.show().ratingKey
                except Exception:
                    pass

        prev_ep = next_ep = None
        if item.type == 'episode':
            siblings = list(item.show().episodes())
            idx = next((i for i, e in enumerate(siblings) if e.ratingKey == item.ratingKey), None)
            if idx is not None:
                if idx > 0:
                    prev_ep = siblings[idx - 1]
                if idx < len(siblings) - 1:
                    next_ep = siblings[idx + 1]

        return render_template('player.html',
                               item=item,
                               stream_url=stream_url,
                               view_offset=view_offset,
                               duration_ms=duration_ms,
                               resumable=resumable,
                               prev_ep=prev_ep,
                               next_ep=next_ep,
                               streams=streams,
                               show_rating_key=show_rating_key)
    except NotFound:
        abort(404, "Media not found.")

def _build_stream_url(rating_key, offset_ms, audio_stream_id=None, subtitle_stream_id=None):
    params = (
        f"hasMDE=1"
        f"&path=/library/metadata/{rating_key}"
        f"&mediaIndex=0&partIndex=0"
        f"&protocol=hls"
        f"&fastSeek=1"
        f"&directPlay=1&directStream=1"
        f"&subtitleSize=100&audioBoost=100"
        f"&X-Plex-Token={config.PLEX_TOKEN}"
        f"&X-Plex-Client-Identifier=plex-offline-launcher"
        f"&X-Plex-Product=PlexOfflineLauncher"
        f"&X-Plex-Version=1.0"
        f"&X-Plex-Platform=Chrome"
        f"&offset={offset_ms // 1000}"
    )
    if audio_stream_id is not None:
        params += f"&audioStreamID={audio_stream_id}"
    if subtitle_stream_id is not None:
        params += f"&subtitleStreamID={subtitle_stream_id}"
    return f"{config.PLEX_URL}/video/:/transcode/universal/start.m3u8?{params}"

@app.route('/api/stream_url/<int:rating_key>')
@login_required
def api_stream_url(user_plex, rating_key):
    """Return a fresh stream URL with a new audio/subtitle selection."""
    offset_ms          = int(request.args.get('offset_ms', 0))
    audio_stream_id    = request.args.get('audio_id',    None)
    subtitle_stream_id = request.args.get('subtitle_id', None)
    if audio_stream_id    is not None: audio_stream_id    = int(audio_stream_id)
    if subtitle_stream_id is not None: subtitle_stream_id = int(subtitle_stream_id)
    if subtitle_stream_id == 0:        subtitle_stream_id = None
    url = _build_stream_url(rating_key, offset_ms,
                            audio_stream_id=audio_stream_id,
                            subtitle_stream_id=subtitle_stream_id)
    return jsonify({'stream_url': url})

@app.route('/api/scrobble/<int:rating_key>', methods=['POST'])
@login_required
def scrobble(user_plex, rating_key):
    data      = request.get_json(silent=True) or {}
    offset_ms = data.get('offset_ms', 0)
    state     = data.get('state', 'playing')
    try:
        params = {
            'ratingKey': rating_key,
            'key':       f'/library/metadata/{rating_key}',
            'state':     state,
            'time':      int(offset_ms),
            'duration':  data.get('duration_ms', 0),
            'X-Plex-Token':              config.PLEX_TOKEN,
            'X-Plex-Client-Identifier': 'plex-offline-launcher',
            'X-Plex-Product':           'PlexOfflineLauncher',
            'X-Plex-Version':           '1.0',
        }
        try:
            params['X-Plex-Token'] = user_plex._token
        except Exception:
            pass
        requests.get(f"{config.PLEX_URL}/:/timeline", params=params, timeout=5)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/search')
@login_required
def search(user_plex):
    query   = request.args.get('query', '').strip()
    results = []
    if query:
        results = enrich(user_plex.search(query))
    return render_template('search_results.html',
                           query=query,
                           results=results,
                           server_title=server_title,
                           is_online=get_internet_status())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
