# Karaoke AI DJ MCP Server & Obot Integration

The Karaoke AI DJ MCP Server bridges your 18,000+ local track library, harmonic analysis, lyrics sentiment, and desktop playback control to AI agent platforms like **Obot**, **Ollama**, and **Claude**.

## Architecture

```mermaid
flowchart LR
    subgraph Host[Host Desktop (Linux / ROCm)]
        Ollama[Ollama\nqwen3:latest / ROCm 890M\n:11434]
        MCP[karaoke-mcp\nMCP Server (SSE)\n:8888]
        PG[(PostgreSQL\nkaraoke DB\n18k tracks + Camelot)]
        Player[Desktop Playback\nSpotify / Chromium / TUI]
    end

    subgraph Cluster[kind-karaoke Cluster]
        Obot[Obot MCP Gateway\n:30080]
    end

    Tunnel[obot tunnel\nkaraoke-obot-tunnel.service]

    Obot -->|Model inference\nhttp://172.18.0.1:11434| Ollama
    Tunnel -->|outbound, authenticated| Obot
    Tunnel --> MCP
    MCP --> PG
    MCP --> Player
    Client[MCP client\nClaude Code / Claude Desktop] -->|direct SSE\nlocalhost:8888/sse| MCP
    Client -.->|or via gateway, for audit| Obot
```

Obot rejects any MCP server URL that resolves to a private IP, so it cannot
reach `:8888` directly. The `obot tunnel` process on the host opens an
authenticated outbound connection instead, and the gateway routes through it.

## Available MCP Tools

| Tool | Purpose | Key Parameters |
|---|---|---|
| `search_songs` | Search tracks in the library with Camelot codes, BPM, energy, and lyrics | `query`, `genre`, `key`, `min_bpm`, `max_bpm`, `only_synced_lyrics` |
| `get_now_playing` | Inspect currently playing audio on Spotify/Chromium/VLC/MPRIS | — |
| `suggest_next_tracks` | Harmonic transitions (Camelot wheel) or energy-up / cool-down | `track_id`, `strategy` (`harmonic`, `energy_up`, `cool_down`, `acoustic`) |
| `analyze_lyric_vibe` | Lyric mood arc, sentiment breakdown, and singer delivery tips | `track_id` |
| `get_dj_stats` | Play counts, top artists, crowd favorites, and discovery history | — |
| `play_track` | Trigger playback via the host karaoke player | `track_id`, `prefer_audio_only` |

## Service Management

The MCP server runs as a systemd `--user` service:

```bash
systemctl --user status karaoke-mcp     # Check service status
systemctl --user restart karaoke-mcp    # Restart service
make mcp                               # Run manually in foreground
```

## Connecting Obot to the DJ

1. **Access Obot**:
   Open [http://172.18.0.2:30080](http://172.18.0.2:30080) in your browser.
2. **Configure Model Provider (Ollama)**:
   * Go to **Model Providers** -> **Ollama**.
   * Set **Host** to `http://172.18.0.1:11434`.
   * Enable model `qwen3:latest` (or `qwen3:1.7b` for ultra-low latency).
3. **Start the tunnel** (required — see the note under Architecture):
   * In Obot, go to **MCP Servers -> Tunnels -> Create MCP Tunnel**.
   * Set **Allowed URLs** to `http://127.0.0.1:8888/*` so the tunnel can reach
     nothing else on the host.
   * Save the one-time secret into `~/.config/obot/tunnel.env` as
     `OBOT_TUNNEL_TOKEN`, then `systemctl --user enable --now karaoke-obot-tunnel`.
   * Verify the tunnel shows **Connected** before continuing.
4. **Register the Karaoke MCP Server**:
   * Go to **MCP Servers -> Add MCP Server -> Remote Server**.
   * **Exact URL**: `http://127.0.0.1:8888/sse`
   * **Tunnel**: select the tunnel from step 3. Without this the save fails
     validation.

## Talking to the DJ

Obot Community is an MCP gateway, not a chat product: it has no agent builder
and no chat window. Point an MCP client at the server instead.

Directly, bypassing the gateway:

```bash
claude mcp add --scope local --transport sse karaoke http://localhost:8888/sse
```

Or through Obot when you want its audit log and access policies in the path.

Either way the client supplies the persona. A system prompt that works well:

> *"You are an energetic, charismatic Karaoke DJ and MC. You know every track in
> our 18,000+ song library. Use harmonic mixing (Camelot wheel) and BPM pacing to
> keep the room dancing. Suggest songs that match the singers' energy, warn them
> about tough high notes, and hype up the crowd!"*

Note that `search_songs(query=...)` is a substring match on artist/title/album,
not semantic search. Vibe phrasing like "90s rock anthem" returns nothing —
filter on `genre`, `min_bpm`/`max_bpm`, `key` and `only_synced_lyrics` instead.
