#!/usr/bin/env python
"""Run folder_scan across several worker processes.

A single scan is decode-bound and runs at about 15 seconds a track, which is
twenty hours for a five thousand file library. The work is embarrassingly
parallel -- each file is independent, and the upsert means two workers landing
on the same artist create one track, not two -- so the only real constraints
are memory and thread oversubscription.

Each worker loads its own CLAP model, so memory is roughly a gigabyte a
worker. Each also sits on top of numpy and librosa, which thread internally,
so every worker is capped to a few threads: four workers each grabbing all 24
cores is slower than four workers taking six each.

Files are sharded round-robin rather than in contiguous blocks, so one worker
does not get handed an album of twelve-minute live tracks while another gets
the singles.

Usage::

    python scripts/parallel_scan.py ~/Music
    python scripts/parallel_scan.py ~/Music --workers 6
    python scripts/parallel_scan.py /run/media/tina/DISK/Music --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import tags  # noqa: E402

DEFAULT_WORKERS = 4

#: Threads per worker. Workers x threads should stay under the core count.
THREADS_PER_WORKER = 4

WORKER_SOURCE = """
import json, sys
from karaoke import folder_scan
paths = set(json.load(open(sys.argv[1])))
stats = folder_scan.scan_and_ingest_folder(
    sys.argv[2], only_paths=paths,
    resolve_streaming=False, use_fingerprint=False,
)
print("WORKER_STATS " + json.dumps({k: v for k, v in stats.items() if k != "items"}))
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", help="directory to scan")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N files (0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    files = [str(p) for p in sorted(root.rglob("*")) if p.is_file() and tags.is_audio(p)]
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("no audio files found")
        return 0

    workers = max(1, min(args.workers, len(files)))
    shards: list[list[str]] = [[] for _ in range(workers)]
    for i, f in enumerate(files):
        shards[i % workers].append(f)

    print(f"{len(files)} audio files under {root}")
    print(f"{workers} workers, {THREADS_PER_WORKER} threads each, "
          f"~{len(files)//workers} files per worker")
    if args.dry_run:
        for i, shard in enumerate(shards):
            print(f"  worker {i}: {len(shard)} files, first {Path(shard[0]).name}")
        return 0

    tmp = Path("/tmp") / f"karaoke-scan-{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)

    env_caps = {
        "OMP_NUM_THREADS": str(THREADS_PER_WORKER),
        "OPENBLAS_NUM_THREADS": str(THREADS_PER_WORKER),
        "MKL_NUM_THREADS": str(THREADS_PER_WORKER),
        "NUMEXPR_NUM_THREADS": str(THREADS_PER_WORKER),
        # Each worker having its own tokenizer pool on top of everything else
        # is what tips a 24-core box into thrashing.
        "TOKENIZERS_PARALLELISM": "false",
    }

    import os

    procs = []
    started = time.time()
    for i, shard in enumerate(shards):
        shard_file = tmp / f"shard-{i}.json"
        shard_file.write_text(json.dumps(shard))
        log_file = open(tmp / f"worker-{i}.log", "w")
        env = {**os.environ, **env_caps}
        procs.append((i, subprocess.Popen(
            [sys.executable, "-c", WORKER_SOURCE, str(shard_file), str(root)],
            stdout=log_file, stderr=subprocess.STDOUT, env=env,
        ), log_file))
        print(f"  worker {i} started (pid {procs[-1][1].pid}, {len(shard)} files)")

    print(f"\nlogs in {tmp}")
    totals: dict[str, int] = {}
    for i, proc, log_file in procs:
        proc.wait()
        log_file.close()
        text = (tmp / f"worker-{i}.log").read_text()
        line = next((l for l in text.splitlines() if l.startswith("WORKER_STATS ")), None)
        if not line:
            print(f"  worker {i}: no stats (exit {proc.returncode}) -- see {tmp}/worker-{i}.log")
            continue
        stats = json.loads(line[len("WORKER_STATS "):])
        print(f"  worker {i}: {stats}")
        for k, v in stats.items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v

    elapsed = time.time() - started
    print(f"\nTOTAL {totals}")
    done = totals.get("processed", 0)
    print(f"{elapsed/60:.1f} min for {done} processed"
          + (f" ({elapsed/done:.1f}s/track effective)" if done else ""))
    print("run `python scripts/vector_backup.py backup` to protect the new vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
