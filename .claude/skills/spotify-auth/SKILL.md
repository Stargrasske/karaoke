---
name: spotify-auth
description: How karaoke authenticates to Spotify and YouTube, and the quota rules for the Spotify Web API. Use when touching spotify_client, adding Spotify API calls, debugging a 429/auth failure, or when playback is signed out.
user-invocable: true
---

# Spotify and playback auth

Two independent things must be signed in, and both fail silently.

## 1. The Spotify Web API — Hermes OAuth

Credentials live in `~/.hermes/auth.json` under `providers.spotify`
(`auth_type: oauth_pkce`; override the path with `HERMES_AUTH_JSON`).
`karaoke.spotify_client.SpotifyClient` reads them and refreshes the access token
via the refresh-token grant automatically.

- **Do not** add a second OAuth flow, a login prompt, or `spotipy`. It is
  already wired; `SpotifyClient()` is the entry point.
- **Do not** print, log, or copy token values. `scripts/auth_status.py` reports
  validity only — follow that pattern.

Granted scopes include `user-read-playback-state`, `user-read-currently-playing`
and `user-modify-playback-state`, so exact playback position is available from
the API via `SpotifyClient.current_playback().position_s` — useful when MPRIS
cannot see the player at all (Spotify Connect to a phone or speaker).

## 2. The browser sessions — kiosk Chrome profile

Playback runs in a dedicated profile, `~/.config/google-chrome-kiosk`
(`KIOSK_PROFILE` in the Makefile), launched by `make tui` with `--app=` and
`--remote-debugging-port=9222`. The profile persists logins, but `--app=` mode
has no address bar and no profile menu, so a lapsed session is invisible — it
just shows as ads, no Premium, or a dead Spotify web player.

```
make auth-spotify   # sign in to Spotify in that profile
make auth-youtube   # sign in to YouTube / YT Music (Premium)
make auth-status    # token validity + profile + CDP state
```

The user signs in **by hand** in the window these open. Never read, copy, or
import cookies from any browser profile to synthesise a login — the profile
keeps its own session, which is the entire reason it is separate.

Chrome will not attach a second process to a profile already in use, so the
playback window must be closed first. `make auth-*` detects this and says so
rather than killing it.

## Quota rules — the expensive lesson

Spotify's rate limit is **per `client_id`, over a rolling 30-second window**. It
is identical for user-OAuth and client-credentials tokens.

- **OAuth is never the fix for a 429.** Changing auth cannot raise the limit.
  Only reducing call volume does. This project lost API access for ~24h to
  repeated `search_track` calls in dry runs.
- `search_track` is the expensive endpoint. Never call it in a loop, a dry run,
  a timer, or a render path without consulting the cache first.
- **Cache both outcomes.** A hit is cached as a `sources` row (`kind='spotify'`);
  a miss goes in `spotify_lookups` with `uri IS NULL`. Without the negative
  cache, tracks Spotify does not carry re-search forever. See
  `localcache.spotify_lookup_due` / `record_spotify_lookup`.
- **A 429 is not a miss.** `_search_items` raises `SpotifyRateLimited` precisely
  so callers cannot confuse the two. Never record it as a miss — that poisons
  the cache with a false negative. Stop for the session instead.
- Status checks use `/me` (`current_user_id`), never a search.

## Debugging

`make auth-status` first — it distinguishes a bad token from a signed-out
browser from a stopped playback window, which look identical from the TUI.
