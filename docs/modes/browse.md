# Browse mode

**Auto-selected** when nothing is playing (`detect.classify()` returns `browse`
for no metadata). The idle state: you drive the library list yourself and
opening a song launches it in the browser. This is the old "youtube mode".

## What it does

- Shows the library table (`H` toggles the overlay); pick a row and `Enter`
  opens it.
- **Unconstrained library browsing**: browse all tracks across the entire SQLite
  database (10,000+ tracks) without artificial page cutoffs, with live count
  tracking in the footer toolbar (`tracks (filter · genre)`).
- **Global SQL sorting**: Sort by Artist/Title, Most Played, Least Played,
  Energy (high→low / low→high), BPM, or Musical Key. Sorting is evaluated
  directly in SQLite (`ORDER BY`), ensuring global ranking across the entire database.
- **Genre & Filter Dropdowns**:
  - `Filter`: Toggle between `working` (tracks with approved lyrics), `all`, `staging`, and `spotify`.
  - `Genre`: Filter by broad consensus genre or specific classification, synchronized with the sidebar genre selector.
  - `Sort`: Change sort mode with instant database-wide reordering.
- **Action Toolbar & Keyboard Shortcuts**:
  - `▶ Open (Enter)`: Opens the track in the player / browser and closes the overlay. Deterministically prefers browser-openable YouTube/web sources over Spotify so the browser window handles playback.
  - `➕ Queue (a)`: Adds the selected track to the active playback queue without closing the browse overlay.
  - `✨ Similar (M)`: Discovers acoustically similar tracks using CLAP audio embeddings and adds them to the active queue.
  - `H` / `Esc`: Close the browse overlay.
- If a cached track has no source URL at all, the TUI falls back to a YouTube
  **search** URL for the artist/title.
- Standard YouTube watch links are auto-upgraded to `music.youtube.com` for
  music-focused audio when opened.

## Sources that have words but no URL

Radio discovery and backfill resolve a track against LRCLIB by artist/title,
which fills in the *words* but records no *URL* — so the track looks complete but
`Enter` opens a search page and post-processing can't analyse it. Fill those in:

```bash
karaoke-find-sources --dry-run     # see what it would store
karaoke-find-sources --limit 50    # store them
```

It searches YouTube per track and stores the best match with the same verified
picker the backfill uses, so a wrong video isn't saved. See
[The TUI → Sourcing](../tui.md#sourcing).

## Debugging Enter/open

If a row appears but `Enter` doesn't visibly open it, see
[Workflow → Debugging browse Enter/open behavior](../workflow.md#debugging-browse-enteropen-behavior).
On every `Enter` the TUI logs the row, artist, title, source kind, URL and the
spawned `xdg-open` PID.

## Related

- [Detection flow](../flows.md#detection-flow)
- [The TUI](../tui.md)
