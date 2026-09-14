# Makefile targets
Generated from make help
```text
admin                        Run the backend operations & worker management TUI
analyze                      Detect + store key/BPM for a file (FILE=... ARTIST=... TITLE=...)
api                          Launch the FastAPI library backend (read-only: tracks, lyrics, stats)
audio-check                  Report whether audio analysis is available (and from where)
auth-spotify                 Sign in to Spotify in the playback Chrome profile (persists for the kiosk window)
auth-status                  Report Spotify token validity and playback-window state
auth-youtube                 Sign in to YouTube / YT Music (Premium) in the playback Chrome profile
browse                       Launch the interactive song browser TUI
browse-log                   Follow TUI/open debug logs
celery-flower                Run the Celery/Flower dashboard on http://127.0.0.1:5555
celery-worker                Run the Celery post-processing worker (CLAP/audio sync workflow tasks)
clean                        Remove build artifacts
clean-tools                  Remove cached helper tools
ctrl-api                     Launch the host-side control API (playback; needs a desktop session)
db-cleanup                   Run track deduplication (fuzzy title + duration guard), orphan source auto-fill, and cache healing
db-cleanup-dry-run           Preview duplicate-track merges without writing anything
deps-make2graph              Fetch and build makefile2graph locally
docs                         Build MkDocs site
docs-audit                   Audit documentation for drift (modules, unlinked docs, targets)
docs-confluence-prep         Generate Confluence-friendly docs tree
docs-confluence-publish      Build Confluence export site
docs-live                    Serve MkDocs locally on http://$(DOCS_ADDR)
docs-sync                    Auto-sync documentation (API modules and Makefile targets)
docs-write                   Regenerate generated docs
folder-scan                  Scan a music folder: fingerprint, classify, resolve YT/Spotify, ingest (DIR=... LIMIT=... DRY_RUN=1)
format                       Run formatters
health                       Run the karaoke platform health check (services, ports, cluster, DB)
help                         Show available targets
index-youtube-cache          Add cached YouTube downloads to SQLite so they show in browse
install                      Install dependencies and the karaoke package
install-audio                Install the isolated key/tempo analysis stack (essentia, librosa) into $(AUDIO_VENV)
install-confluence           Install optional Confluence publishing dependencies
k8s-build                    Build the library API container image
k8s-deploy                   Deploy the library API to the kind cluster
k8s-load                     Load the image into the kind cluster
k8s-logs                     Follow library API pod logs
k8s-port-forward             Expose the library API on http://localhost:8080
k8s-seed-db                  Copy the local SQLite library into the cluster PVC
k8s-status                   Show deployed karaoke resources
k8s-undeploy                 Remove the karaoke API from the cluster (keeps the PVC)
lint                         Run lint checks
mic-test                     Live mic VU meter to confirm capture level (SECS=4)
mq-port-forward              Expose the in-cluster RabbitMQ AMQP on localhost:5672 (management on 15672)
postprocess-enqueue-all      Enqueue every track missing key/BPM or word-timing for post-processing
postprocess-worker           Run the host-side post-processing worker (analysis + word-timing)
recording-analyse            Decompile a recording into the DB (ID=...); needs the audio venv
recording-show               Show a recording's derived track list (ID=...)
recordings                   List record-mode sessions
sample                       Detect key/BPM by recording what is playing (SECS=45 ARTIST=... TITLE=...)
stats                        Show play + radio-discovery stats from the local cache
systemd-down                 Stop all karaoke services
systemd-install              Install/refresh the karaoke systemd --user units (symlinks to deploy/systemd)
systemd-status               Show status of all karaoke units + last health check
systemd-uninstall            Stop and remove the karaoke systemd --user units
systemd-up                   Start all karaoke services via the target
test                         Run tests
test-audio                   Verify the audio + identify + lyrics stack (mic, songrec, LRCLIB)
tui                          Launch the clean karaoke control-surface TUI prototype
upgrade-timings              Upgrade cached lyrics to word-level timing via YouTube captions
upgrade-timings-dry-run      Preview which cached tracks can gain word-level timing
vector-index                 Rebuild OpenSearch vector indexes from SQLite (set LINES=1 for line docs)
vector-index-dry-run         Preview SQLite -> OpenSearch vector indexing without writing
vector-status                Show OpenSearch vector index status and rebuild progress
venv                         Create virtual environment
view_makeflow                Open generated SVG locally
web                          Serve the TUI in a web browser using textual-serve
webtui                       Alias for make web
```
