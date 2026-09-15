"""Read-only Docker memory sampling for a bounded demonstration workload."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def memory_bytes(value):
    match = re.fullmatch(r"([0-9.]+)([kKMGT]?i?B)", value.strip())
    if not match:
        raise ValueError("Unexpected Docker memory unit")
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    return int(float(match[1]) * units[match[2]])


def sample(project):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError("Invalid project name")
    ids = subprocess.check_output(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        text=True,
    ).split()
    if not ids:
        return []
    output = subprocess.check_output(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
        text=True,
        timeout=30,
    )
    return [
        {
            "container": row["Name"],
            "memory_bytes": memory_bytes(row["MemUsage"].split("/")[0]),
            "pids": int(row["PIDs"]),
        }
        for line in output.splitlines()
        if line.strip()
        for row in [json.loads(line)]
    ]


def measure(project, duration, output):
    if not 5 <= duration <= 3600:
        raise ValueError("Measure between 5 and 3600 seconds")
    started = time.monotonic()
    samples = []
    errors = 0
    while time.monotonic() - started < duration:
        try:
            records = sample(project)
        except (subprocess.SubprocessError, OSError, ValueError):
            # Containers may be intentionally restarted by lifecycle tests.
            # Keep prior observations and explicitly count missing samples.
            errors += 1
            time.sleep(2)
            continue
        samples.append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "containers": records,
                "total_memory_bytes": sum(r["memory_bytes"] for r in records),
            }
        )
        time.sleep(2)
    result = {
        "project": project,
        "samples": samples,
        "peak_observed_bytes": max(
            (s["total_memory_bytes"] for s in samples), default=0
        ),
        "failed_samples": errors,
        "capacity_certified": False,
        "limitation": "Docker statistics exclude host memory and may miss short peaks. Recheck on the actual VDS under its memory limit.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"Observed container memory peak: {result['peak_observed_bytes'] / 1024**2:.0f} MiB; report: {output}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument(
        "--output", type=Path, default=Path(".local-history/capacity.json")
    )
    args = parser.parse_args()
    measure(args.project, args.seconds, args.output)
