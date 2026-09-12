"""Container CPU/RAM via a restricted read-only Docker API; no host mounts."""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


def measurements(stats):
    cpu = stats["cpu_stats"]["cpu_usage"]["total_usage"] / 1_000_000_000
    memory = stats["memory_stats"]
    usage = memory.get("usage", 0)
    # Docker's Linux CLI subtracts inactive file cache from total memory usage.
    detail = memory.get("stats", {})
    cache = detail.get("total_inactive_file", detail.get("inactive_file", 0))
    working = usage - cache if cache < usage else usage
    return cpu, usage, working, memory.get("limit", 0)


class DockerMetrics:
    def __init__(self, project, endpoint="http://docker-proxy:2375"):
        self.project = project
        self.endpoint = endpoint
        self.lock = threading.Lock()

    def get(self, path, **params):
        response = requests.get(self.endpoint + path, params=params, timeout=(2, 3))
        response.raise_for_status()
        return response.json()

    def read_container(self, container):
        identifier = container.get("Id", "")
        labels = container.get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", identifier)
            or labels.get("com.docker.compose.project") != self.project
            or not service
            or labels.get("com.docker.compose.oneoff", "false").lower() == "true"
        ):
            return None
        try:
            values = measurements(
                self.get(
                    f"/containers/{identifier}/stats",
                    stream="false",
                    **{"one-shot": "true"},
                )
            )
            return [identifier, service], values
        except (requests.RequestException, KeyError, TypeError, ValueError):
            return [identifier, service], None

    def collect(self):
        if not self.lock.acquire(blocking=False):
            metric = GaugeMetricFamily(
                "docker_stats_up", "Docker inventory was collected"
            )
            metric.add_metric([], 0)
            yield metric
            return
        try:
            yield from self.collect_once()
        finally:
            self.lock.release()

    def collect_once(self):
        families = [
            CounterMetricFamily(
                "container_cpu_usage_seconds_total",
                "Docker cumulative CPU time",
                labels=["id", "service"],
            ),
            GaugeMetricFamily(
                "container_memory_usage_bytes",
                "Docker total memory usage",
                labels=["id", "service"],
            ),
            GaugeMetricFamily(
                "container_memory_working_set_bytes",
                "Memory usage excluding inactive file cache",
                labels=["id", "service"],
            ),
            GaugeMetricFamily(
                "container_spec_memory_limit_bytes",
                "Container memory limit",
                labels=["id", "service"],
            ),
        ]
        container_up = GaugeMetricFamily(
            "docker_container_stats_up",
            "Container statistics were collected",
            labels=["id", "service"],
        )
        available = GaugeMetricFamily(
            "docker_stats_up", "Docker inventory was collected"
        )
        try:
            containers = self.get(
                "/containers/json",
                filters=json.dumps(
                    {"label": [f"com.docker.compose.project={self.project}"]}
                ),
            )
            # Bound work even if an unexpectedly large host matches the label.
            if not isinstance(containers, list) or len(containers) > 64:
                raise ValueError("Unexpected container inventory")
            with ThreadPoolExecutor(max_workers=8) as pool:
                for result in pool.map(self.read_container, containers):
                    if result is None:
                        continue
                    labels, values = result
                    container_up.add_metric(labels, int(values is not None))
                    if values is not None:
                        for family, value in zip(families, values):
                            family.add_metric(labels, value)
            available.add_metric([], 1)
        except (requests.RequestException, ValueError, TypeError):
            available.add_metric([], 0)
        yield available
        yield container_up
        yield from families


def main():
    REGISTRY.register(DockerMetrics(os.environ["COMPOSE_PROJECT_NAME"]))
    start_http_server(9103)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
