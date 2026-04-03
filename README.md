# Plex Offline Launcher

A family-friendly, self-hosted web client for your Plex Media Server — designed for full functionality during internet outages. Multi-user, zero CDN dependencies, with resume, scrobbling, auto-play, and PWA support.

**Last Updated:** April 2026

---

## The Problem

Plex relies on plex.tv for authentication. If your internet goes down, most Plex clients lock you out — even though the server is sitting right there on your local network.

## The Solution

A self-hosted web interface that runs directly on your Plex server. It uses Python and Flask to connect via a pre-authorised token, bypassing any need for an internet connection.

---

## Features

**Playback**
- HLS streaming via locally-bundled hls.js — no CDN, works fully offline
- Resume from where you left off, per user
- Progress scrobbling every 10s — On Deck stays accurate across all Plex clients
- Audio track and subtitle track switching mid-playback without a page reload
- Auto-play next episode with 15s countdown and cancel

**Navigation**
- Who's Watching? user selector with proxied avatars (works offline)
- Dynamic home dashboard — Continue Watching and Recently Added
- Library browsing with infinite scroll pagination (no hanging on large libraries)
- Sort by: A–Z, Z–A, Recently Added, Oldest Added, Newest Release, Top Rated
- Unwatched-only filter toggle
- Search across all libraries
- "Next to Watch" button on show pages (uses Plex On Deck)
- Extras and trailers surfaced on movie and show detail pages

**Player controls**
- Custom seek bar with hover time tooltip and touch scrubbing (mobile)
- Volume slider and mute toggle
- ±10s skip buttons, fullscreen toggle
- Keyboard shortcuts: Space/K, ←/→, ↑/↓, F, M, N

**Offline resilience**
- Zero external dependencies at runtime — no Google Fonts, no CDN scripts
- Internet status indicator updated live every 30s (background thread + client polling)
- User avatars proxied locally — browser-cached for offline visits
- PWA: installable to home screen on Android and iOS, app shell cached by service worker

**Reliability**
- Per-user watch history, progress bars, and watched/unwatched status everywhere
- Friendly error pages for 404, 500, and Plex connection failures with a setup checklist
- Safe fallbacks for older plexapi versions (totalSize, grandparentRatingKey)
- Library nav always populated on every page via Flask context processor

---

## Project Structure

```
plex-offline-launcher/
├── src/
│   ├── static/
│   │   ├── icons/
│   │   │   ├── icon-192.png      ← PWA home screen icon
│   │   │   └── icon-512.png
│   │   ├── js/
│   │   │   └── hls.min.js        ← bundled locally, never fetched from CDN
│   │   ├── manifest.json         ← PWA manifest
│   │   └── sw.js                 ← service worker (app shell cache)
│   ├── templates/
│   │   ├── base.html
│   │   ├── error.html
│   │   ├── home_dashboard.html
│   │   ├── item_details.html
│   │   ├── library.html
│   │   ├── player.html
│   │   ├── search_results.html
│   │   └── user_select.html
│   ├── app.py
│   ├── config.py
│   └── requirements.txt
└── README.md
```

---

## Installation & Setup

### 1. Install Dependencies

```bash
cd src
pip install -r requirements.txt
```

### 2. Configure `config.py`

```python
PLEX_URL   = 'http://192.168.1.100:32400'  # Your server's local IP and port
PLEX_TOKEN = 'YourPlexTokenHere'            # Admin token (see below)
SECRET_KEY = 'any-long-random-string-here'  # For session security
```

**Finding your Plex token:**
1. Open Plex Web, go to any media item
2. Click `···` → Get Info → View XML
3. Copy the `X-Plex-Token=` value from the URL

### 3. Run

```bash
flask --app app run --host=0.0.0.0
```

Or directly: `python app.py`

### 4. Access

```
http://<your_server_ip>:5000
```

To install as a home screen app: open in Chrome (Android) or Safari (iOS) → Share → Add to Home Screen.

---

## How Resume & Scrobbling Work

The player reports playback position to Plex every 10 seconds, on pause, and on page close. This keeps On Deck accurate across all Plex clients and ensures resuming picks up from the right spot.

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / K | Play / Pause |
| ← / → | Seek ±10 seconds |
| ↑ / ↓ | Volume ±10% |
| F | Toggle fullscreen |
| M | Toggle mute |
| N | Skip to next episode |

---

## Troubleshooting

**Can't connect from another device?** Add an inbound firewall rule for TCP port 5000.

**Stream won't play?** Plex must be running. Direct play is requested first; Plex transcodes if needed.

**No users shown?** Check `PLEX_TOKEN` is the admin token.

**Avatars not loading?** They proxy through the local server from plex.tv — load online, browser-cached offline.

---

## License

MIT License
