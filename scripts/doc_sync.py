#!/usr/bin/env python3
"""scripts/doc_sync.py - Documentation drift auditor and synchronization tool.

Audits and synchronizes:
1. Python module docstrings in docs/api.md (via mkdocstrings).
2. MkDocs navigation coverage for markdown files in docs/.
3. Makefile target documentation in docs/makefile-targets.md.

Usage:
    python scripts/doc_sync.py --check     # Return exit code 1 if drift detected
    python scripts/doc_sync.py --fix       # Auto-update docs/api.md & docs/makefile-targets.md
    python scripts/doc_sync.py --json      # Output audit report as JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def find_repo_root() -> Path:
    """Locate the repository root directory."""
    current = Path(__file__).resolve().parent.parent
    if (current / "mkdocs.yml").exists() and (current / "src" / "karaoke").exists():
        return current
    cwd = Path.cwd().resolve()
    if (cwd / "mkdocs.yml").exists() and (cwd / "src" / "karaoke").exists():
        return cwd
    return current


def get_python_modules(repo_root: Path) -> list[str]:
    """Return all public Python module names in src/karaoke (excluding __init__.py)."""
    karaoke_dir = repo_root / "src" / "karaoke"
    if not karaoke_dir.exists():
        return []
    modules: list[str] = []
    for p in sorted(karaoke_dir.glob("*.py")):
        if p.stem.startswith("__"):
            continue
        modules.append(p.stem)
    return modules


def get_api_doc_modules(repo_root: Path) -> list[str]:
    """Parse docs/api.md and extract all ::: karaoke.<module> directives."""
    api_doc = repo_root / "docs" / "api.md"
    if not api_doc.exists():
        return []
    content = api_doc.read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*:::\s*karaoke\.([a-zA-Z0-9_]+)", re.MULTILINE)
    return sorted(pattern.findall(content))


def extract_nav_files(nav_entry: Any) -> list[str]:
    """Recursively extract relative doc paths from mkdocs.yml nav structure."""
    files: list[str] = []
    if isinstance(nav_entry, str):
        if nav_entry.endswith(".md"):
            files.append(nav_entry)
    elif isinstance(nav_entry, list):
        for item in nav_entry:
            files.extend(extract_nav_files(item))
    elif isinstance(nav_entry, dict):
        for _, val in nav_entry.items():
            files.extend(extract_nav_files(val))
    return files


def get_mkdocs_nav(repo_root: Path) -> tuple[list[str], list[str]]:
    """Return referenced doc files and raw nav text from mkdocs.yml."""
    mkdocs_file = repo_root / "mkdocs.yml"
    if not mkdocs_file.exists():
        return [], []
    content = mkdocs_file.read_text(encoding="utf-8")

    # Look for nav: section and parse simple paths (with or without yaml dependency)
    referenced_files: list[str] = []
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(content)
        if isinstance(data, dict) and "nav" in data:
            referenced_files = extract_nav_files(data["nav"])
    except Exception:
        # Fallback regex extraction of .md references
        md_matches = re.findall(r"[\s:-]+\s*([a-zA-Z0-9_./-]+\.md)", content)
        referenced_files = [m.strip() for m in md_matches]

    return sorted(set(referenced_files)), content.splitlines()


def get_doc_files(repo_root: Path) -> list[str]:
    """Find all markdown files under docs/ relative to docs/ (excluding generated/)."""
    docs_dir = repo_root / "docs"
    if not docs_dir.exists():
        return []
    files: list[str] = []
    for p in sorted(docs_dir.rglob("*.md")):
        rel = p.relative_to(docs_dir).as_posix()
        if rel.startswith("generated/"):
            continue
        files.append(rel)
    return files


def get_makefile_targets(repo_root: Path) -> dict[str, str]:
    """Extract documented targets and comments from Makefile."""
    makefile = repo_root / "Makefile"
    if not makefile.exists():
        return {}
    targets: dict[str, str] = {}
    pattern = re.compile(r"^([a-zA-Z0-9_.-]+):.*?##\s*(.+)$")
    for line in makefile.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            target, help_text = match.groups()
            targets[target.strip()] = help_text.strip()
    return targets


def audit_documentation(repo_root: Path) -> dict[str, Any]:
    """Run full audit of documentation against codebase."""
    py_modules = get_python_modules(repo_root)
    api_modules = get_api_doc_modules(repo_root)
    missing_api_modules = [m for m in py_modules if m not in api_modules]
    stale_api_modules = [m for m in api_modules if m not in py_modules]

    doc_files = get_doc_files(repo_root)
    nav_files, _ = get_mkdocs_nav(repo_root)
    unlinked_docs = [d for d in doc_files if d not in nav_files]
    broken_nav_links = [
        n for n in nav_files if not (repo_root / "docs" / n).exists()
    ]

    makefile_targets = get_makefile_targets(repo_root)
    targets_doc = repo_root / "docs" / "makefile-targets.md"
    missing_make_targets: list[str] = []
    targets_doc_content = (
        targets_doc.read_text(encoding="utf-8") if targets_doc.exists() else ""
    )
    for target in makefile_targets:
        if not re.search(rf"\b{re.escape(target)}\b", targets_doc_content):
            missing_make_targets.append(target)

    drift_detected = bool(
        missing_api_modules
        or stale_api_modules
        or unlinked_docs
        or broken_nav_links
        or missing_make_targets
    )

    return {
        "drift_detected": drift_detected,
        "total_python_modules": len(py_modules),
        "documented_api_modules": len(api_modules),
        "missing_api_modules": missing_api_modules,
        "stale_api_modules": stale_api_modules,
        "total_doc_files": len(doc_files),
        "unlinked_docs": unlinked_docs,
        "broken_nav_links": broken_nav_links,
        "total_makefile_targets": len(makefile_targets),
        "missing_makefile_targets": missing_make_targets,
    }


def human_readable_title(module_name: str) -> str:
    """Format module name into a readable documentation section title."""
    title_map = {
        "admin_tui": "Admin & Operator TUI",
        "analyze": "Audio Key & Tempo Analysis",
        "api": "FastAPI Library REST API",
        "api_client": "FastAPI Client Library",
        "audio_vector": "Audio Feature Vector Embeddings",
        "autoclassify": "Automated Track Classification",
        "backfill": "Collection Metadata Backfill",
        "backfill_runner": "Backfill Queue Runner",
        "beats": "Beat Tracking & Rhythm Analysis",
        "bigtext": "Large Terminal Text Rendering",
        "browse": "Interactive Library Browser TUI",
        "caption_sync": "Closed Caption & Subtitle Synchronization",
        "celery_app": "Celery Task Queue & Orchestration",
        "clap_vector": "CLAP Audio Genre Vectors",
        "cli": "Command-Line Interface",
        "config": "Configuration & Environment Management",
        "coverart": "Album Artwork Fetching & Embedding",
        "cover_store": "Artwork Storage & Caching",
        "ctrl_api": "Host Playback Control API",
        "detect": "Audio Fingerprinting & Recognition",
        "embed": "Text & Lyric Embeddings",
        "events": "Event Hooks & Pub/Sub System",
        "find_sources": "Streaming & Local Source Resolution",
        "folder_scan": "Collection & Directory Scanning",
        "genre": "Genre Tagging & Classification",
        "identify": "Shazam / SongRec Live Identification",
        "librarysearch": "OpenSearch & SQLite Search Engine",
        "librarystats": "Library & Playback Statistics",
        "localcache": "Local SQLite Database & Caching",
        "lockfile": "Process Synchronization & File Locking",
        "logger": "Structured Logging & Diagnostics",
        "lyric_align": "Lyric Alignment & Timings",
        "lyric_language": "Language Detection for Lyrics",
        "lyrics": "Lyrics Fetching & LRC Parsing",
        "moodart": "Mood-Based Visual Artwork",
        "moodframe": "Mood Video & Animation Framing",
        "musictheory": "Musical Scales, Keys, and Theory Helpers",
        "osclient": "OpenSearch Client & Index Management",
        "player": "Terminal Karaoke Player & Synced Display",
        "player_follow": "Follow-Mode Playback Controller",
        "player_open": "Browser Kiosk & Media Player Launcher",
        "player_sync": "Synced Playback State Coordination",
        "playerctl": "MPRIS / Media Controller Client",
        "postprocess_queue": "Post-Processing Task Queue",
        "postprocess_status": "Worker & Pipeline Status Reporting",
        "postprocess_worker": "Background Post-Processing Worker",
        "queue_suggest": "Smart Queue & Recommendation Engine",
        "radio_pipeline": "Radio Session Recording & Library Import",
        "recorder": "Live Audio Capture & PipeWire Recording",
        "recording_audio": "Audio Segment Processing for Recordings",
        "recording_slice": "FLAC Slicing & Export for Recordings",
        "recording_worker": "Recording Cut & Transcode Worker",
        "sample_audio": "Audio Sampling & Quick Key/BPM Inspection",
        "scanner": "File Scanner & Search Indexer",
        "search": "Lyric & Audio Search CLI / Helpers",
        "sentiment": "Sentiment Analysis & Lyric Mood Coloring",
        "silence": "Silence Trimming & Audio Preprocessing",
        "smartlist": "Smart Playlists & Query Filters",
        "source_select": "Source Priority & Selection Rules",
        "spotify_client": "Spotify Web API Client",
        "spotify_import": "Spotify Playlist & Track Importer",
        "spotify_playlist": "Spotify Playlist Sync & Management",
        "stage_sources": "Staged Downloads & Pre-Ingestion",
        "staging": "Staging Directory & File State",
        "staging_api": "Staging Control API",
        "tags": "Audio Metadata Tagging & ID3",
        "tasks": "Celery / Background Tasks Definition",
        "tone": "Tone Analysis & Mood Scoring",
        "track_analysis": "Multi-Modal Track Analysis Coordinator",
        "tui": "Karaoke Terminal User Interface",
        "upgrade_timings": "LRC Word-Timing Upgrade Engine",
        "vector_index": "OpenSearch Vector Indexing & KNN Search",
        "visuals": "Rich Terminal Visualizations & Meters",
        "web": "Web Interface & HTML Endpoints",
        "whisper_clean": "Whisper Transcript Post-Processing",
        "whisper_sync": "Whisper Speech-to-Text Word Alignment",
        "youtube": "YouTube Playback & Cache Resolution",
        "ytmusic_client": "YouTube Music API Client",
        "ytmusic_lyrics": "YouTube Music Synced Lyrics Fetcher",
        "ytmusic_playlist": "YouTube Music Playlist Synchronization",
    }
    return title_map.get(module_name, module_name.replace("_", " ").title())


def fix_api_docs(repo_root: Path, missing_modules: list[str]) -> int:
    """Append missing module autodoc blocks to docs/api.md."""
    if not missing_modules:
        return 0

    api_doc = repo_root / "docs" / "api.md"
    content = api_doc.read_text(encoding="utf-8") if api_doc.exists() else "# API reference\n\n"

    # Find where custom REST API sections begin (e.g. ## `GET /api/workers/status`)
    # We want to place Python module references before REST API endpoint documentation if possible
    marker = re.search(r"^##\s+`GET\s+", content, re.MULTILINE)
    
    new_sections: list[str] = []
    for mod in sorted(missing_modules):
        title = human_readable_title(mod)
        new_sections.append(f"\n## {title}\n\n::: karaoke.{mod}\n")

    combined_blocks = "".join(new_sections)

    if marker:
        insert_idx = marker.start()
        updated_content = content[:insert_idx] + combined_blocks + "\n" + content[insert_idx:]
    else:
        updated_content = content.rstrip() + "\n" + combined_blocks + "\n"

    api_doc.write_text(updated_content, encoding="utf-8")
    return len(missing_modules)


def fix_makefile_targets_doc(repo_root: Path) -> bool:
    """Regenerate docs/makefile-targets.md from Makefile targets."""
    targets = get_makefile_targets(repo_root)
    if not targets:
        return False

    targets_doc = repo_root / "docs" / "makefile-targets.md"
    lines = [
        "# Makefile targets",
        "Generated from make help",
        "```text",
    ]
    for target in sorted(targets.keys()):
        lines.append(f"{target:<28} {targets[target]}")
    lines.append("```\n")

    targets_doc.write_text("\n".join(lines), encoding="utf-8")
    return True


def print_report(results: dict[str, Any], fixed_api: int = 0, fixed_targets: bool = False) -> None:
    """Print human-readable audit report."""
    print("=================================================================")
    print("             Karaoke Documentation Drift Audit Report            ")
    print("=================================================================")
    print(f"Total Python Modules: {results['total_python_modules']}")
    print(f"Documented Modules in docs/api.md: {results['documented_api_modules']}")

    if results["missing_api_modules"]:
        print(f"\n[!] Missing Modules in docs/api.md ({len(results['missing_api_modules'])}):")
        for m in results["missing_api_modules"]:
            print(f"    - karaoke.{m}")
    else:
        print("\n[ok] All Python modules are documented in docs/api.md")

    if results["stale_api_modules"]:
        print(f"\n[!] Stale Modules in docs/api.md ({len(results['stale_api_modules'])}):")
        for m in results["stale_api_modules"]:
            print(f"    - karaoke.{m}")

    print(f"\nTotal Markdown Doc Files: {results['total_doc_files']}")
    if results["unlinked_docs"]:
        print(f"[!] Unlinked Markdown Files in mkdocs.yml ({len(results['unlinked_docs'])}):")
        for d in results["unlinked_docs"]:
            print(f"    - docs/{d}")
    else:
        print("[ok] All Markdown files are linked in mkdocs.yml")

    if results["broken_nav_links"]:
        print(f"[!] Broken Navigation Links in mkdocs.yml ({len(results['broken_nav_links'])}):")
        for b in results["broken_nav_links"]:
            print(f"    - docs/{b} (file not found)")

    print(f"\nTotal Documented Makefile Targets: {results['total_makefile_targets']}")
    if results["missing_makefile_targets"]:
        print(f"[!] Targets missing from docs/makefile-targets.md ({len(results['missing_makefile_targets'])}):")
        for t in results["missing_makefile_targets"]:
            print(f"    - make {t}")
    else:
        print("[ok] docs/makefile-targets.md is up-to-date with Makefile")

    if fixed_api > 0 or fixed_targets:
        print("\n-----------------------------------------------------------------")
        print("Fix Actions Applied:")
        if fixed_api > 0:
            print(f"  + Appended {fixed_api} missing modules to docs/api.md")
        if fixed_targets:
            print("  + Regenerated docs/makefile-targets.md with all Makefile targets")

    print("=================================================================")
    if results["drift_detected"] and not (fixed_api and fixed_targets):
        print("Status: DRIFT DETECTED (run with --fix or update mkdocs.yml)")
    else:
        print("Status: CLEAN / SYNCHRONIZED")
    print("=================================================================")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Karaoke documentation drift auditor and synchronization tool."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Audit documentation and return non-zero exit code if drift is found.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix missing modules in docs/api.md and sync docs/makefile-targets.md.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Custom repository root path.",
    )

    args = parser.parse_args()
    repo_root = args.root.resolve() if args.root else find_repo_root()

    audit = audit_documentation(repo_root)

    fixed_api = 0
    fixed_targets = False
    if args.fix:
        if audit["missing_api_modules"]:
            fixed_api = fix_api_docs(repo_root, audit["missing_api_modules"])
        if audit["missing_makefile_targets"]:
            fixed_targets = fix_makefile_targets_doc(repo_root)
        # Re-audit after fixes
        audit = audit_documentation(repo_root)

    if args.json:
        audit["fixed_api_modules"] = fixed_api
        audit["fixed_makefile_targets"] = fixed_targets
        print(json.dumps(audit, indent=2))
    else:
        print_report(audit, fixed_api=fixed_api, fixed_targets=fixed_targets)

    if args.check and audit["drift_detected"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
