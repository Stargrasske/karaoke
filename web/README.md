# Karaoke dashboard

Angular 19 operator dashboard for the karaoke library and desktop control APIs.

From the repository root, activate a Python 3.11+ environment with the project
installed, install frontend dependencies once, and start the complete stack:

```bash
cd web
npm install
cd ..
python scripts/dev.py
```

Open `http://localhost:4200`. The seeded Player, Recording, and Operations
dashboards provide library search, queue and playback control, recording and
sample capture, worker/log monitoring, folder scans, audio cuts, player-window
management, and the stage view. Each widget supports styled and ASCII variants;
layouts are draggable, resizable, and persisted in local storage.

For frontend-only work, run `npm start` in this directory. API URLs are loaded
from `public/config.json` and default to ports 8000 and 8765.

## Checks

```bash
npm test -- --watch=false
npm run build
```

Use Angular CLI 19 for generators. The project pins `angular-gridster2` 19 to
remain compatible with Angular 19.
