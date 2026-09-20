"""Plan Higgsfield visual plates and deterministic karaoke lyric overlays.

Higgsfield supplies mood-driven background footage. Lyrics and audio-reactive
visuals remain local: generated video models are not a reliable subtitle clock,
while ASS/FFmpeg can honor the cached line and word timings exactly.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Optional

from . import localcache
from .lyrics import parse_enhanced_lrc
from .sentiment import mood_of

MAX_GENERATED_CLIP_S = 12.0
MIN_EPISODE_S = 1.0
GAP_MIN_S = 6.0


@dataclass(frozen=True)
class VisualEpisode:
    index: int
    start_s: float
    end_s: float
    kind: str
    mood: str
    energy: float
    brightness: float
    bass_drive: float
    treble_drive: float
    prompt: str

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


def _prompt(kind: str, mood: str, energy: float, brightness: float) -> str:
    motion = "slow drifting camera" if energy < 0.35 else "rhythmic fluid camera motion"
    texture = "deep low-frequency shapes" if brightness < 0.45 else "fine luminous high-frequency textures"
    episode = {
        "intro": "opening instrumental atmosphere",
        "instrumental": "wordless instrumental passage",
        "outro": "resolving instrumental outro",
        "vocals": "expressive abstract music visual",
    }.get(kind, "abstract music visual")
    return (
        f"{episode}, {mood} emotional tone, {motion}, {texture}, "
        "cinematic 16:9 background plate, strong depth, no people singing, "
        "no typography, no captions, no logos, seamless motion"
    )


def build_visual_plan(
    synced_lyrics: str,
    duration_s: float,
    *,
    energy: Optional[float] = None,
    brightness: Optional[float] = None,
) -> list[VisualEpisode]:
    lines, ends, _ = parse_enhanced_lrc(synced_lyrics)
    if not lines:
        return []
    safe_energy = max(0.0, min(1.0, float(energy if energy is not None else 0.5)))
    safe_brightness = max(0.0, min(1.0, float(brightness if brightness is not None else 0.5)))
    bass = 0.55 * safe_energy + 0.45 * (1.0 - safe_brightness)
    treble = 0.55 * safe_brightness + 0.45 * safe_energy

    raw: list[tuple[float, float, str, str]] = []
    if lines[0][0] >= MIN_EPISODE_S:
        raw.append((0.0, lines[0][0], "intro", "neutral"))

    for index, (start, text) in enumerate(lines):
        next_start = lines[index + 1][0] if index + 1 < len(lines) else duration_s
        explicit_end = ends.get(index)
        word_cap = min(12.0, max(1.5, max(1, len(text.split())) * 0.7))
        vocal_end = min(value for value in (explicit_end, start + word_cap, next_start) if value is not None)
        raw.append((start, max(start + MIN_EPISODE_S, vocal_end), "vocals", mood_of(text)))
        if next_start - vocal_end >= GAP_MIN_S:
            kind = "outro" if index + 1 == len(lines) else "instrumental"
            raw.append((vocal_end, next_start, kind, "neutral"))

    episodes: list[VisualEpisode] = []
    for start, end, kind, mood in raw:
        cursor = start
        while cursor < end - 0.01:
            clip_end = min(end, cursor + MAX_GENERATED_CLIP_S)
            episodes.append(VisualEpisode(
                index=len(episodes),
                start_s=round(cursor, 3),
                end_s=round(clip_end, 3),
                kind=kind,
                mood=mood,
                energy=round(safe_energy, 3),
                brightness=round(safe_brightness, 3),
                bass_drive=round(bass, 3),
                treble_drive=round(treble, 3),
                prompt=_prompt(kind, mood, safe_energy, safe_brightness),
            ))
            cursor = clip_end
    return episodes


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def build_ass(synced_lyrics: str, *, duration_s: float) -> str:
    lines, ends, word_times = parse_enhanced_lrc(synced_lyrics)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Aptos Display,64,&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,120,120,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for index, (start, text) in enumerate(lines):
        next_start = lines[index + 1][0] if index + 1 < len(lines) else duration_s
        end = min(ends.get(index, next_start), next_start)
        if end <= start:
            end = min(duration_s, start + 4.0)
        words = text.split()
        starts = word_times.get(index, [])
        karaoke_text = ""
        if starts and len(starts) == len(words):
            for word_index, word in enumerate(words):
                word_end = starts[word_index + 1] if word_index + 1 < len(starts) else end
                karaoke_text += rf"{{\k{max(1, round((word_end - starts[word_index]) * 100))}}}{_ass_escape(word)} "
        else:
            per_word = max(1, round(((end - start) / max(1, len(words))) * 100))
            karaoke_text = "".join(rf"{{\k{per_word}}}{_ass_escape(word)} " for word in words)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Karaoke,,0,0,0,karaoke,{karaoke_text.strip()}"
        )
    return header + "\n".join(events) + "\n"


def higgsfield_commands(episodes: list[VisualEpisode], output_dir: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    prompts: set[str] = set()
    for episode in episodes:
        if episode.prompt in prompts:
            continue
        prompts.add(episode.prompt)
        commands.append([
            "higgsfield", "generate", "create", "seedance_2_0",
            "--prompt", episode.prompt,
            "--duration", str(int(MAX_GENERATED_CLIP_S)),
            "--aspect_ratio", "16:9",
            "--resolution", "720p",
            "--generate_audio", "false",
            "--wait", "--json",
        ])
    return commands


def export_package(track_id: int, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    from . import track_analysis
    with localcache.connect() as conn:
        track_analysis.ensure_schema(conn)
        row = conn.execute(
            """SELECT t.artist, t.title, t.duration, l.synced_lyrics,
                      a.bpm, a.energy, a.brightness
               FROM tracks t
               JOIN lyrics l ON l.track_id=t.track_id AND l.kind='approved'
               LEFT JOIN track_analysis a ON a.track_id=t.track_id
               WHERE t.track_id=?""",
            (track_id,),
        ).fetchone()
    if row is None or not row["synced_lyrics"]:
        raise ValueError("track needs approved synchronized lyrics")
    duration = float(row["duration"] or 0.0)
    if duration <= 0:
        parsed, _, _ = parse_enhanced_lrc(row["synced_lyrics"])
        duration = (parsed[-1][0] + 8.0) if parsed else 0.0
    episodes = build_visual_plan(
        row["synced_lyrics"], duration,
        energy=row["energy"], brightness=row["brightness"],
    )
    ass_path = output_dir / "lyrics.ass"
    ass_path.write_text(build_ass(row["synced_lyrics"], duration_s=duration), encoding="utf-8")
    commands = higgsfield_commands(episodes, output_dir)
    manifest = {
        "track_id": track_id,
        "artist": row["artist"],
        "title": row["title"],
        "duration_s": duration,
        "bpm": row["bpm"],
        "energy": row["energy"],
        "brightness": row["brightness"],
        "episodes": [asdict(episode) for episode in episodes],
        "higgsfield_commands": [shlex.join(command) for command in commands],
        "visual_plate_count": len(commands),
        "lyrics_ass": str(ass_path),
        "composition": {
            "background": "Higgsfield episode clips, looped/trimmed to episode duration",
            "lyrics": "ASS karaoke overlay",
            "audio_reactivity": "FFmpeg showwaves/showspectrum from the local source audio",
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a Higgsfield-backed synchronized lyrics video")
    parser.add_argument("track_id", type=int)
    parser.add_argument("--output", type=Path, default=Path("lyrics-video"))
    parser.add_argument("--generate", action="store_true", help="Submit every visual episode to Higgsfield")
    args = parser.parse_args(argv)
    manifest = export_package(args.track_id, args.output)
    print(f"Wrote {args.output / 'manifest.json'} and {args.output / 'lyrics.ass'}")
    if args.generate:
        for index, command in enumerate(higgsfield_commands(
            [VisualEpisode(**episode) for episode in manifest["episodes"]], args.output
        )):
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            (args.output / f"plate-{index:02d}.json").write_text(result.stdout, encoding="utf-8")
    else:
        print("Generation not submitted. Review the manifest, then rerun with --generate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
