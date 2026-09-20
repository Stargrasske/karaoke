# ASCII Dance Pocket Library

96 short, editable ASCII animations: **48 solo loops + 48 couple loops**, across **24 feelings**. Each feeling has two solo dances and two coordinated couple dances. Four visual character types rotate through the collection: round, cat, robot, and sprite.

## Start here

1. Unzip this folder.
2. Install Python 3.9 or newer if it is not already available.
3. Open a terminal in this folder.
4. Run `python player.py` for the interactive preview. On macOS/Linux, use `python3` if needed; on Windows, `py` also works.

No packages, account, network connection, API keys, or generation service are needed to run these files. A monospace terminal with ANSI support is recommended. These are actual ASCII text frames, not AI-rendered videos. No audio is included.

```sh
# Play a solo. Ctrl+C stops playback.
python player.py play joyful-solo-01

# Play a couple.
python player.py play romantic-couple-01

# Slow down or speed up.
python player.py play angry-solo-01 --speed 0.65
python player.py play sleepy-couple-02 --speed 1.5

# Play a short playlist exactly once.
python player.py play shy-solo-01 flirty-couple-02 joyful-couple-01 --loops 1

# Browse.
python player.py list
python player.py list --feeling dreamy
python player.py list --mode couple
```

## Combine loops

Make a sequence without flattening the editable text:

```sh
python player.py combine shy-solo-01 romantic-couple-01 joyful-couple-01 --layout sequence -o story.json
python player.py play story.json
```

Put two loops next to each other:

```sh
python player.py combine joyful-solo-01 joyful-solo-02 --layout side -o party.json
python player.py play party.json
```

The side layout plays each clip at its original timing. Shorter clips repeat to fill the longest clip; the final boundary can cut a shorter loop. Pair equal-duration clips for an aligned loop seam. Side-by-side output is 82 columns wide by default; widen your terminal or set `--gap 0`. Sequences inherit the largest input dimensions.

## Edit a dance

- Open `text/<clip-id>.txt` to see all eight poses without JSON escape characters. These are convenient drawing/copy references.
- Edit `clips/<clip-id>.json` to change the animation the player reads.
- `frames`: eight strings, each with 14 lines of 40 characters. Preserve spaces; they are part of the positioning.
- `durations_ms`: one positive duration per frame. Larger values slow the move; unequal values create holds or emphasis.
- `duration_ms`: informational total. Update it after changing timings. The player uses `durations_ms` as the timing source.
- Use ordinary ASCII characters only. A backslash is written as `\\` in JSON and displays as one backslash.
- For easy hand editing without escaping, use the included `edit_frames.py` importer/exporter.
- Spaces act as empty cells. There is no baked image background, but transparency is a renderer's choice.

```sh
# Export just the drawing frames, separated by ---FRAME--- marker lines.
python edit_frames.py export clips/joyful-solo-01.json editable-frames.txt

# Edit the text file in a plain-text editor, then import it.
# Separate frames with a line containing exactly ---FRAME---.
python edit_frames.py import editable-frames.txt clips/joyful-solo-01.json custom.json
python player.py play custom.json
```

You can change faces, add hats, alter poses, copy frames between dances, or stretch pauses. Keep a fixed 40x14 canvas for easiest mixing. Files are deliberately plain and have no animation-tool lock-in.

### Regenerating the whole library

`build_pack.py` contains the pose geometry, sentiment mappings, formations, and timing profiles. Edit that source, then run:

```sh
python build_pack.py
```

Warning: rebuilding overwrites the generated `clips/`, `text/`, catalog, and combined library. Save custom work under separate names or outside those directories first. Edits to the aggregate `dance-library.json` do not automatically update the individual clip files.

## What's included

- `clips/`: 96 independent JSON animations.
- `text/`: 96 readable pose strips, with frame timing labels.
- `dance-library.json`: all clips in one portable data file.
- `player.py`: interactive terminal preview, playlists, speed control, sequence/side compositor.
- `edit_frames.py`: round-trip between JSON and easily edited text frames.
- `build_pack.py`: the original editable generator.
- `validate_pack.py`: checks the collection and exercises the tools.
- `CATALOG.md`: all 96 dance names grouped by feeling.
- `SAMPLE-POSES.txt`: six static couple examples.

## Motion design notes

Each base loop lasts 1.04-3.20 seconds, with eight frames and a 40-column by 14-row canvas. Timing varies by feeling: energetic loops are quick; shy/surprised loops pause; sad and sleepy loops are slower and lower. Character roots hop, sway, shuffle, dip, or stomp, while pose sequences animate arms and footwork.

Couple variant 1 maintains a shared inside-hand contact point. Couple variant 2 mirrors the partners with a two-frame response delay and an opening/closing formation. Variants share a reusable pose vocabulary; these are stylized expressive gestures, not anatomically accurate dance instruction. At only eight frames, playback has an intentionally stepped ASCII look.

All frames use printable ASCII plus newlines. Names, faces, timing, choreography, and source are editable. The collection is finite: 96 distinct loop files, not an unlimited generation subscription.
