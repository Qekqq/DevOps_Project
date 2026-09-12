import requests

from src.docker_metrics_exporter import DockerMetrics, measurements


def test_cpu_seconds_and_linux_memory_cache():
    sample = {
        "cpu_stats": {"cpu_usage": {"total_usage": 2_500_000_000}},
        "memory_stats": {"usage": 1000, "limit": 2000, "stats": {"inactive_file": 300}},
    }
    assert measurements(sample) == (2.5, 1000, 700, 2000)
    sample["memory_stats"]["stats"] = {"total_inactive_file": 200}
    assert measurements(sample)[2] == 800
    sample["memory_stats"]["stats"] = {"inactive_file": 1100}
    assert measurements(sample)[2] == 1000


def test_inventory_filters_foreign_projects_and_failed_stats(monkeypatch):
    collector = DockerMetrics("test")
    own = {
        "Id": "a" * 64,
        "Labels": {
            "com.docker.compose.project": "test",
            "com.docker.compose.service": "api",
        },
    }
    foreign = {
        "Id": "b" * 64,
        "Labels": {
            "com.docker.compose.project": "other",
            "com.docker.compose.service": "db",
        },
    }
    calls = []

    def get(path, **params):
        calls.append(path)
        if path == "/containers/json":
            return [own, foreign]
        raise requests.ConnectionError()

    monkeypatch.setattr(collector, "get", get)
    metrics = {item.name: item for item in collector.collect()}
    assert calls == ["/containers/json", f"/containers/{'a' * 64}/stats"]
    assert metrics["docker_container_stats_up"].samples[0].value == 0
    assert not metrics["container_cpu_usage_seconds"].samples
