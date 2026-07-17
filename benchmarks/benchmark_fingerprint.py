from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from mini_code_agent.workspace import WorkspaceFingerprinter, WorkspaceSnapshot


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("runs must be at least 1")
    return parsed


def _timed_capture(
    fingerprinter: WorkspaceFingerprinter,
) -> tuple[float, WorkspaceSnapshot]:
    started = time.perf_counter()
    snapshot = fingerprinter.capture()
    snapshot.fingerprint
    return time.perf_counter() - started, snapshot


def benchmark(root: Path, runs: int) -> dict[str, int | float | list[float]]:
    cold_seconds: list[float] = []
    snapshot: WorkspaceSnapshot | None = None
    for _ in range(runs):
        elapsed, snapshot = _timed_capture(WorkspaceFingerprinter(root))
        cold_seconds.append(elapsed)

    warm_fingerprinter = WorkspaceFingerprinter(root)
    warm_fingerprinter.capture().fingerprint
    warm_seconds = [
        _timed_capture(warm_fingerprinter)[0] for _ in range(runs)
    ]

    assert snapshot is not None
    return {
        "entries": len(snapshot.files),
        "cold_seconds": cold_seconds,
        "cold_median_seconds": statistics.median(cold_seconds),
        "warm_seconds": warm_seconds,
        "warm_median_seconds": statistics.median(warm_seconds),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark secure workspace fingerprint capture."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--runs", type=_positive_int, default=5)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.root, args.runs), sort_keys=True))


if __name__ == "__main__":
    main()
