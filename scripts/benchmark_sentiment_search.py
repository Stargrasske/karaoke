#!/usr/bin/env python3
"""Benchmark OpenSearch query performance for keyword, semantic, and filter-cached hybrid search."""
from __future__ import annotations

import time
from typing import Callable, Any

from karaoke import search
from karaoke.osclient import client


def measure(fn: Callable[[], Any], iterations: int = 5) -> tuple[float, float, Any]:
    """Run `fn` for `iterations`, return (first_run_ms, avg_subsequent_ms, last_result)."""
    latencies: list[float] = []
    res = None
    for i in range(iterations):
        t0 = time.perf_counter()
        res = fn()
        latencies.append((time.perf_counter() - t0) * 1000.0)
    cold_ms = latencies[0]
    warm_ms = sum(latencies[1:]) / len(latencies[1:]) if len(latencies) > 1 else cold_ms
    return cold_ms, warm_ms, res


def main() -> int:
    c = client()
    if not c.indices.exists(index="tracks"):
        print("Index 'tracks' does not exist in OpenSearch!")
        return 1

    count = c.count(index="tracks").get("count", 0)
    print(f"=== OpenSearch Benchmark on index 'tracks' ({count:,} total documents) ===")

    queries = ["love", "sunshine", "darkness", "summer dance", "heartbreak"]
    moods = ["happy", "tender", "sad", "angry", "neutral"]

    print(f"\n{'Query':<15} {'Type':<18} {'Mood':<10} {'Cold (ms)':<12} {'Warm Avg (ms)':<14} {'Hits':<6}")
    print("-" * 75)

    for q, m in zip(queries, moods):
        # 1. Unfiltered keyword
        cold, warm, hits = measure(lambda: search.keyword_search(q, k=10, os_client=c))
        print(f"{q:<15} {'Keyword (unfiltered)':<18} {'—':<10} {cold:<12.2f} {warm:<14.2f} {len(hits):<6}")

        # 2. Filtered keyword with Lucene bitset cache
        cold, warm, hits = measure(lambda: search.keyword_search(q, k=10, mood=m, os_client=c))
        print(f"{q:<15} {'Keyword (filtered)':<18} {m:<10} {cold:<12.2f} {warm:<14.2f} {len(hits):<6}")

        # 3. Hybrid search (semantic + keyword + filter cache)
        cold, warm, hits = measure(lambda: search.hybrid_search(q, k=10, mood=m, os_client=c))
        print(f"{q:<15} {'Hybrid (filtered)':<18} {m:<10} {cold:<12.2f} {warm:<14.2f} {len(hits):<6}")
        print("-" * 75)

    print("\nBenchmark completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
