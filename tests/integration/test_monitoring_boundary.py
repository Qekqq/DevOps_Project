"""Exercise the Docker API boundary from the collector's private network."""

import json
import os
import subprocess


def test_monitoring_has_no_host_filesystem_or_docker_mutation_access():
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    project = compose[compose.index("-p") + 1]
    assert project != "devops_project", "This test only runs on an isolated stack"

    def inspect(service):
        return json.loads(
            subprocess.check_output(
                ["docker", "inspect", f"{project}-{service}-1"],
                text=True,
            )
        )[0]

    grafana = inspect("grafana")
    assert grafana["HostConfig"]["ReadonlyRootfs"]
    assert not grafana["HostConfig"]["PortBindings"]
    assert "GF_PLUGINS_PREINSTALL_DISABLED=true" in grafana["Config"]["Env"]
    assert "GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false" in grafana["Config"]["Env"]
    assert not any(
        mount["RW"] and mount["Destination"] != "/var/lib/grafana"
        for mount in grafana["Mounts"]
        if mount["Type"] != "tmpfs"
    )
    plugins = subprocess.check_output(
        ["docker", "exec", grafana["Id"], "ls", "-1", "/opt/grafana-plugins"],
        text=True,
    ).splitlines()
    assert set(plugins) == {"loki", "prometheus"}
    logs = subprocess.check_output(
        ["docker", "logs", grafana["Id"]], text=True, stderr=subprocess.STDOUT
    )
    assert 'msg="Installing plugin"' not in logs
    alloy = inspect("alloy")
    assert not alloy["HostConfig"]["Privileged"]
    assert not (
        {
            "/rootfs",
            "/sys",
            "/var/run/docker.sock",
            "/var/lib/docker",
            "/run/containerd",
        }
        & {m["Destination"] for m in alloy["Mounts"]}
    )
    stats = inspect("docker-stats")
    assert not stats["Mounts"]
    proxy = inspect("docker-proxy")
    assert not proxy["HostConfig"]["PortBindings"]
    assert proxy["HostConfig"]["ReadonlyRootfs"]
    assert set(proxy["NetworkSettings"]["Networks"]) == {project + "_docker-observe"}
    probe = """
import requests
base = 'http://docker-proxy:2375'
assert requests.get(base + '/_ping', timeout=3).status_code == 200
assert requests.get(base + '/containers/json', timeout=3).status_code == 200
for path in ['/info', '/containers/' + 'a'*64 + '/json', '/containers/' + 'a'*64 + '/archive?path=/etc', '/images/json', '/volumes']:
    assert requests.get(base + path, timeout=3).status_code == 403
assert requests.get(base + '/%2e%2e/containers/json', timeout=3).status_code in (400, 403)
assert requests.post(base + '/containers/create', json={}, timeout=3).status_code == 403
assert requests.delete(base + '/containers/' + 'a'*64, timeout=3).status_code == 403
print('Docker API boundary verified')
"""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            project + "_docker-observe",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--cpus",
            "0.25",
            stats["Image"],
            "python",
            "-c",
            probe,
        ],
        check=True,
    )


def test_metrics_database_role_cannot_read_accounts_or_write():
    compose = json.loads(os.environ["TEST_COMPOSE_COMMAND"])
    project = compose[compose.index("-p") + 1]
    assert project != "devops_project"
    probe = """
from sqlalchemy.exc import ProgrammingError
from src.db.database import get_engine
with get_engine().connect() as db:
    assert db.exec_driver_sql('SELECT current_user').scalar() == 'diabetes_metrics'
    assert db.exec_driver_sql('SELECT count(*) FROM studies').scalar() >= 0
    db.rollback()
    for statement in ['SELECT password_hash FROM users LIMIT 1', 'UPDATE studies SET patient_code = patient_code WHERE false']:
        db.exec_driver_sql('SET TRANSACTION READ WRITE')
        try:
            db.exec_driver_sql(statement)
        except ProgrammingError as error:
            assert error.orig.pgcode == '42501'
        else:
            raise AssertionError('Unexpected database privilege')
        finally:
            db.rollback()
print('Metrics database permissions verified')
"""
    subprocess.run(
        ["docker", "exec", f"{project}-metrics-exporter-1", "python", "-c", probe],
        check=True,
    )
