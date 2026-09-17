SHELL := /bin/bash

VENV ?= .venv
PYTHON := PYTHONPATH=src $(VENV)/bin/python
MKDOCS := $(VENV)/bin/mkdocs
# mkdocs defaults to :8000, which karaoke-api already listens on (systemd --user),
# so serving the docs there fails to bind. Default to 127.0.0.1:8085 (free port).
DOCS_ADDR ?= 127.0.0.1:8085
AUDIO_VENV ?= .venv-audio
AUDIO_PY := $(AUDIO_VENV)/bin/python

# Dedicated Chrome profile for the playback window. Kept separate from the
# everyday browser so its logins (Spotify, YouTube Premium) persist unattended,
# and shared by `tui` and the `auth-*` targets so signing in once is enough.
KIOSK_PROFILE ?= $(HOME)/.config/google-chrome-kiosk
CHROME ?= google-chrome

TOOLS_DIR := .tools/bin
CACHE_DIR := .cache
MAKE2GRAPH_DIR := $(CACHE_DIR)/makefile2graph
MAKE2GRAPH_BIN := $(TOOLS_DIR)/make2graph
MAKE2GRAPH_REPO := https://github.com/lindenb/makefile2graph.git
MAKE2GRAPH_REF ?= master

# Deployment (kind cluster)
IMAGE ?= karaoke-api:dev
KIND_CLUSTER ?= karaoke
KUBE_CONTEXT ?= kind-karaoke
K8S_NAMESPACE ?= karaoke

.DEFAULT_GOAL := help

.PHONY: help venv install install-confluence docs docs-live docs-write docs-audit docs-sync docs-confluence-prep \
        docs-confluence-publish deps-make2graph view_makeflow lint format \
        test test-audio mic-test stats clean clean-tools browse tui browse-log \
        install-audio analyze api ctrl-api \
        k8s-build k8s-load k8s-deploy k8s-seed-db k8s-status k8s-logs k8s-undeploy \
        upgrade-timings upgrade-timings-dry-run \
        index-youtube-cache db-cleanup db-cleanup-dry-run vector-index vector-index-dry-run vector-status folder-scan \
        mq-port-forward postprocess-worker celery-worker celery-flower postprocess-enqueue-all \
        systemd-install systemd-uninstall systemd-up systemd-down systemd-status health \
        auth-spotify auth-youtube auth-status sample audio-check \
        recordings recording-show recording-analyse

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "%-28s %s\n", $$1, $$2}'

venv: ## Create virtual environment
	python3 -m venv $(VENV)

install: venv ## Install dependencies and the karaoke package
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e ".[dev]"

install-confluence: install ## Install optional Confluence publishing dependencies
	$(PYTHON) -m pip install -r requirements-confluence.txt

install-audio: ## Install the isolated key/tempo analysis stack (essentia, librosa) into $(AUDIO_VENV)
	python3 -m venv $(AUDIO_VENV)
	$(AUDIO_PY) -m pip install --upgrade pip
	$(AUDIO_PY) -m pip install -r requirements-audio.txt
	@echo "[ok] audio stack ready in $(AUDIO_VENV) — set KARAOKE_AUDIO_PYTHON=$(PWD)/$(AUDIO_PY)"

deps-make2graph: ## Fetch and build makefile2graph locally
	@mkdir -p "$(TOOLS_DIR)" "$(CACHE_DIR)"
	@if [ ! -d "$(MAKE2GRAPH_DIR)/.git" ]; then \
		git clone "$(MAKE2GRAPH_REPO)" "$(MAKE2GRAPH_DIR)"; \
	fi
	@git -C "$(MAKE2GRAPH_DIR)" fetch --tags --force origin
	@git -C "$(MAKE2GRAPH_DIR)" checkout "$(MAKE2GRAPH_REF)"
	@$(MAKE) -C "$(MAKE2GRAPH_DIR)"
	@cp "$(MAKE2GRAPH_DIR)/make2graph" "$(MAKE2GRAPH_BIN)"
	@chmod +x "$(MAKE2GRAPH_BIN)"
	@echo "[ok] make2graph ready at $(MAKE2GRAPH_BIN)"

docs: ## Build MkDocs site
	$(MKDOCS) build

docs-live: ## Serve MkDocs locally on http://$(DOCS_ADDR)
	$(MKDOCS) serve --dev-addr $(DOCS_ADDR)

docs-audit: ## Audit documentation for drift (modules, unlinked docs, targets)
	$(PYTHON) scripts/doc_sync.py --check

docs-sync: ## Auto-sync documentation (API modules and Makefile targets)
	$(PYTHON) scripts/doc_sync.py --fix

docs-write: deps-make2graph ## Regenerate generated docs
	@mkdir -p docs/generated docs/assets || true
	echo  '# Makefile targets' > docs/makefile-targets.md
	echo 'Generated from make help' >> docs/makefile-targets.md
	echo '```text' >> docs/makefile-targets.md
	make help >> docs/makefile-targets.md
	echo '```' >> docs/makefile-targets.md
	@LC_ALL=C $(MAKE) -Bnd | "$(MAKE2GRAPH_BIN)" --format M > docs/generated/makeflow.mmd
	@LC_ALL=C $(MAKE) -Bnd | "$(MAKE2GRAPH_BIN)" --format d > docs/generated/makeflow.dot

	@if command -v dot >/dev/null 2>&1; then \
		dot -Tsvg docs/generated/makeflow.dot > docs/assets/makeflow.svg; \
		echo "[ok] wrote docs/assets/makeflow.svg"; \
	else \
		echo "[warn] graphviz 'dot' not found; skipping SVG render"; \
	fi

docs-confluence-prep: ## Generate Confluence-friendly docs tree
	$(PYTHON) tools/snippet_expander.py

docs-confluence-publish: docs-confluence-prep ## Build Confluence export site
	$(MKDOCS) build -f mkdocs-confluence.yml

view_makeflow: ## Open generated SVG locally
	xdg-open docs/assets/makeflow.svg

lint: ## Run lint checks
	$(PYTHON) -m compileall -q src tests

format: ## Run formatters
	@echo "Add ruff format / prettier here"

test: ## Run tests
	$(PYTHON) -m pytest -v

test-audio: ## Verify the audio + identify + lyrics stack (mic, songrec, LRCLIB)
	scripts/soundcheck.sh

mic-test: ## Live mic VU meter to confirm capture level (SECS=4)
	scripts/soundcheck.sh --meter $(or $(SECS),4)

stats: ## Show play + radio-discovery stats from the local cache
	$(PYTHON) -c "import sys; from karaoke.cli import stats_main; sys.argv=['karaoke-stats']; raise SystemExit(stats_main())"

clean-tools: ## Remove cached helper tools
	rm -rf "$(TOOLS_DIR)" "$(CACHE_DIR)"

clean: ## Remove build artifacts
	rm -rf site docs_confluence build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	
browse: ## Launch the interactive song browser TUI
	$(PYTHON) -m karaoke.browse

tui: ## Launch the clean karaoke control-surface TUI prototype
	$(PYTHON) -m karaoke.tui

sample: ## Detect key/BPM by recording what is playing (SECS=45 ARTIST=... TITLE=...)
	$(PYTHON) -m karaoke.sample_audio --seconds $${SECS:-45} \
		$${ARTIST:+--artist "$(ARTIST)"} $${TITLE:+--title "$(TITLE)"}

recordings: ## List record-mode sessions
	$(PYTHON) -m karaoke.recording_worker --list

recording-show: ## Show a recording's derived track list (ID=...)
	$(PYTHON) -m karaoke.recording_worker --show $(ID)

recording-analyse: ## Decompile a recording into the DB (ID=...); needs the audio venv
	PYTHONPATH=src $(AUDIO_PY) -m karaoke.recording_worker --analyse $(ID)

analyze: ## Detect + store key/BPM for a file (FILE=... ARTIST=... TITLE=...)
	$(PYTHON) -c "import sys; from karaoke.cli import analyze_main; sys.exit(analyze_main(['--file','$(FILE)','--artist','$(ARTIST)','--title','$(TITLE)']))"

auth-spotify: ## Sign in to Spotify in the playback Chrome profile (persists for the kiosk window)
	@$(MAKE) --no-print-directory _auth-window URL="https://accounts.spotify.com/login" WHAT=Spotify

auth-youtube: ## Sign in to YouTube / YT Music (Premium) in the playback Chrome profile
	@$(MAKE) --no-print-directory _auth-window URL="https://accounts.google.com/ServiceLogin?service=youtube" WHAT="YouTube (Premium)"

# Shared body. Chrome refuses to attach a second process to a profile already in
# use, so a running kiosk window has to be closed first -- say so plainly rather
# than killing it, which would drop whatever is playing.
_auth-window:
	@if ss -lnt | grep -q :9222; then \
		echo "The playback window is running (CDP :9222)."; \
		echo "Close it first, then re-run this target -- Chrome cannot open"; \
		echo "a second window against the same profile."; \
		exit 1; \
	fi
	@echo "Opening $(WHAT) sign-in in $(KIOSK_PROFILE)."
	@echo "Sign in by hand, then close the window; the session persists for 'make tui'."
	@$(CHROME) --user-data-dir=$(KIOSK_PROFILE) "$(URL)" >/dev/null 2>&1 &

audio-check: ## Report whether audio analysis is available (and from where)
	@$(PYTHON) -c "from karaoke import analyze; \
	  local = analyze.stack_available(); other = analyze.audio_python(); \
	  print('in-process DSP stack :', 'yes' if local else 'no'); \
	  print('audio venv fallback  :', other or 'not found'); \
	  print('analysis available   :', 'yes' if (local or other) else \
	        'NO -- run: make install-audio')"

auth-status: ## Report Spotify token validity and playback-window state
	$(PYTHON) scripts/auth_status.py

browse-log: ## Follow TUI/open debug logs
	tail -f "$${XDG_DATA_HOME:-$$HOME/.local/share}/karaoke/logs/karaoke.log" "$${XDG_DATA_HOME:-$$HOME/.local/share}/karaoke/logs/xdg-open.stderr.log"

api: ## Launch the FastAPI library backend (read-only: tracks, lyrics, stats)
	$(PYTHON) -m karaoke.api

ctrl-api: ## Launch the host-side control API (playback; needs a desktop session)
	$(PYTHON) -m karaoke.ctrl_api

k8s-build: ## Build the library API container image
	# --network=host: the default docker bridge has no working DNS on this host,
	# so pip cannot resolve pypi.org during the build without it.
	docker build --network=host -t $(IMAGE) -f deploy/Dockerfile .

k8s-load: k8s-build ## Load the image into the kind cluster
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)

k8s-deploy: k8s-load ## Deploy the library API to the kind cluster
	kubectl --context $(KUBE_CONTEXT) apply -k deploy/k8s
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) rollout status deploy/karaoke-api --timeout=120s

k8s-seed-db: ## Copy the local SQLite library into the cluster PVC
	./scripts/seed_db.sh

k8s-status: ## Show deployed karaoke resources
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) get all,pvc

k8s-port-forward: ## Expose the library API on http://localhost:8080
	# The pre-existing kind cluster only maps the OpenSearch NodePort. Recreating
	# it to add a mapping would destroy the OpenSearch release, so forward instead.
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) port-forward svc/karaoke-api 8080:8000

k8s-logs: ## Follow library API pod logs
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) logs -f deploy/karaoke-api

k8s-undeploy: ## Remove the karaoke API from the cluster (keeps the PVC)
	kubectl --context $(KUBE_CONTEXT) delete -k deploy/k8s --ignore-not-found

index-youtube-cache: ## Add cached YouTube downloads to SQLite so they show in browse
	$(PYTHON) scripts/index_youtube_cache.py

folder-scan: ## Scan a music folder: fingerprint, classify, resolve YT/Spotify, ingest (DIR=... LIMIT=... DRY_RUN=1)
	$(PYTHON) -c "import sys; from karaoke.cli import folder_scan_main; args=['$(DIR)']+(['--limit','$(LIMIT)'] if '$(LIMIT)' else [])+(['--dry-run'] if '$(DRY_RUN)' else []); raise SystemExit(folder_scan_main(args))"

db-cleanup: ## Run track deduplication (fuzzy title + duration guard), orphan source auto-fill, and cache healing
	$(PYTHON) scripts/db_cleanup.py

db-cleanup-dry-run: ## Preview duplicate-track merges without writing anything
	$(PYTHON) scripts/db_cleanup.py --dry-run

mq-port-forward: ## Expose the in-cluster RabbitMQ AMQP on localhost:5672 (management on 15672)
	# The pre-existing kind cluster has no AMQP port mapping; forward instead of
	# recreating it (which would destroy the OpenSearch release).
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) port-forward svc/rabbitmq 5672:5672 15672:15672

postprocess-worker: ## Run the host-side post-processing worker (analysis + word-timing)
	KARAOKE_ORCHESTRATOR=legacy $(PYTHON) -m karaoke.postprocess_worker

celery-worker: ## Run the Celery post-processing worker (CLAP/audio sync workflow tasks)
	KARAOKE_ORCHESTRATOR=celery PYTHONPATH=src $(VENV)/bin/celery \
		-A karaoke.celery_app:app worker -Q karaoke-postprocess-celery \
		--loglevel=$${LOGLEVEL:-INFO} --concurrency=$${CONCURRENCY:-2} \
		--events

celery-flower: ## Run the Celery/Flower dashboard on http://127.0.0.1:5555
	KARAOKE_ORCHESTRATOR=celery PYTHONPATH=src $(VENV)/bin/celery \
		-A karaoke.celery_app:app flower \
		--address=$${FLOWER_HOST:-127.0.0.1} --port=$${FLOWER_PORT:-5555}

postprocess-enqueue-all: ## Enqueue every track missing key/BPM or word-timing for post-processing
	$(PYTHON) scripts/enqueue_postprocess.py

upgrade-timings-dry-run: ## Preview which cached tracks can gain word-level timing
	$(PYTHON) -m karaoke.upgrade_timings --dry-run

upgrade-timings: ## Upgrade cached lyrics to word-level timing via YouTube captions
	$(PYTHON) -m karaoke.upgrade_timings

vector-index-dry-run: ## Preview SQLite -> OpenSearch vector indexing without writing
	$(PYTHON) -m karaoke.vector_index --dry-run --no-embed --lines

health: ## Run the karaoke platform health check (services, ports, cluster, DB)
	$(PYTHON) scripts/healthcheck.py

webtui: web ## Alias for make web

admin: ## Run the backend operations & worker management TUI
	$(PYTHON) -m karaoke.admin_tui

web: ## Serve the TUI in a web browser using textual-serve
	$(PYTHON) scripts/web_serve.py

systemd-install: ## Install/refresh the karaoke systemd --user units (symlinks to deploy/systemd)
	mkdir -p $(HOME)/.config/systemd/user
	ln -sf $(CURDIR)/deploy/systemd/karaoke-api.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-ctrl-api.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-mq-forward.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-celery-worker.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-celery-flower.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-kiosk.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-webtui.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-relay.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-postprocess@.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-postprocess.slice $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-healthcheck.service $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke-healthcheck.timer $(HOME)/.config/systemd/user/
	ln -sf $(CURDIR)/deploy/systemd/karaoke.target $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable karaoke.target karaoke-healthcheck.timer
	@echo "Installed. Start with: make systemd-up"

systemd-uninstall: ## Stop and remove the karaoke systemd --user units
	-systemctl --user disable --now karaoke.target karaoke-healthcheck.timer
	-systemctl --user stop karaoke-api karaoke-ctrl-api karaoke-mq-forward karaoke-celery-worker karaoke-celery-flower karaoke-kiosk karaoke-webtui karaoke-relay 'karaoke-postprocess@*'
	rm -f $(HOME)/.config/systemd/user/karaoke-*.service \
	      $(HOME)/.config/systemd/user/karaoke-*.timer \
	      $(HOME)/.config/systemd/user/karaoke-*.slice \
	      $(HOME)/.config/systemd/user/karaoke.target
	systemctl --user daemon-reload

systemd-up: ## Start all karaoke services via the target
	systemctl --user start karaoke.target karaoke-healthcheck.timer

systemd-down: ## Stop all karaoke services
	systemctl --user stop karaoke.target

systemd-status: ## Show status of all karaoke units + last health check
	-systemctl --user --no-pager status 'karaoke*' || true
	@echo "--- last health check ---"
	-journalctl --user -u karaoke-healthcheck.service -n 12 --no-pager || true


vector-index: ## Rebuild OpenSearch vector indexes from SQLite (set LINES=1 for line docs)
	$(PYTHON) -m karaoke.vector_index --rebuild $(if $(LINES),--lines,)

vector-status: ## Show OpenSearch vector index status and rebuild progress
	$(PYTHON) -m karaoke.vector_index --status