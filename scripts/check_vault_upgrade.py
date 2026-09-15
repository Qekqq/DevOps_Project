"""Rehearse a Vault file-storage upgrade using only generated test secrets and volumes."""

import argparse
import json
import subprocess
import sys
import time
from uuid import uuid4

from scripts.volume_snapshot import (
    SnapshotError,
    create_snapshot,
    restore_snapshot_to_new_volume,
)

CONFIG = """disable_mlock = true
storage "file" { path = "/vault/file" }
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = true
}
api_addr = "http://vault:8200"
"""
REQUEST = """
import json, sys, requests
request = json.load(sys.stdin)
session = requests.Session()
session.trust_env = False
response = session.request(timeout=10, **request)
print(json.dumps({'status': response.status_code, 'body': response.json() if response.content else None}))
"""


def check_upgrade(old_image, new_image, client_image):
    label = "devops-vault-upgrade-" + uuid4().hex[:12]
    containers, volumes = [], []
    network = None

    def docker(*args, stdin=None):
        result = subprocess.run(
            ["docker", *args],
            input=stdin,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            # Neither CLI arguments nor returned API bodies belong in diagnostics.
            raise RuntimeError(f"Isolated Vault check: Docker {args[0]} failed")
        return result.stdout.strip()

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    def api(method, path, data=None, token=None, expected=200):
        response = json.loads(
            docker(
                "exec",
                "-i",
                client,
                "python",
                "-c",
                REQUEST,
                stdin=json.dumps(
                    {
                        "method": method,
                        "url": "http://vault:8200/v1/" + path,
                        "headers": {"X-Vault-Token": token} if token else {},
                        "json": data,
                    }
                ),
            )
        )
        require(
            response["status"] == expected,
            f"Vault API returned unexpected status {response['status']}",
        )
        return response["body"]

    def wait_sealed():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                return api("GET", "sys/seal-status")
            except RuntimeError:
                time.sleep(1)
        raise RuntimeError("Test Vault did not become ready")

    def start(image, volume, suffix):
        name = label + "-" + suffix
        containers.append(name)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "devops.test=" + label,
            "--network",
            network,
            "--network-alias",
            "vault",
            "--user",
            "100:1000",
            "--cap-drop",
            "ALL",
            "--read-only",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
            "--mount",
            f"type=volume,source={volume},target=/vault/file",
            "--env",
            "TEST_VAULT_CONFIG=" + CONFIG,
            "--entrypoint",
            "/bin/sh",
            image,
            "-ec",
            'printf "%s" "$TEST_VAULT_CONFIG" > /tmp/server.hcl; exec vault server -config=/tmp/server.hcl',
        )
        wait_sealed()
        return name

    def validate(root_token, role_id, secret_id, expected_data):
        data = api("GET", "secret/data/probe", token=root_token)["data"]["data"]
        require(data == expected_data, "Vault secret was not preserved")
        auth = api(
            "POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id}
        )
        token = auth["auth"]["client_token"]
        require(
            api("GET", "secret/data/probe", token=token)["data"]["data"]
            == expected_data,
            "AppRole cannot read its allowed secret",
        )
        api("GET", "secret/data/forbidden", token=token, expected=403)
        api(
            "POST",
            "secret/data/probe",
            {"data": {"tampered": True}},
            token=token,
            expected=403,
        )

    try:
        old_id, new_id, client_id = [
            json.loads(docker("image", "inspect", name))[0]["Id"]
            for name in (old_image, new_image, client_image)
        ]
        network = docker(
            "network", "create", "--internal", "--label", "devops.test=" + label, label
        )
        client = label + "-client"
        containers.append(client)
        docker(
            "run",
            "-d",
            "--name",
            client,
            "--label",
            "devops.test=" + label,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--memory",
            "256m",
            "--cpus",
            "1",
            client_id,
            "python",
            "-c",
            "import time; time.sleep(900)",
        )
        for suffix in ("original",):
            name = label + "-" + suffix
            docker(
                "volume",
                "create",
                "--label",
                "devops.test=" + label,
                "--label",
                "com.docker.compose.project=" + label,
                "--label",
                "com.docker.compose.volume=vault_data",
                name,
            )
            volumes.append(name)
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--mount",
                f"type=volume,source={name},target=/vault/file",
                "--entrypoint",
                "chown",
                old_id,
                "100:1000",
                "/vault/file",
            )
        old = start(old_id, volumes[0], "old")
        old_version = wait_sealed()["version"]
        initialized = api(
            "PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1}
        )
        root_token, key = initialized["root_token"], initialized["keys_base64"][0]
        require(
            not api("PUT", "sys/unseal", {"key": key})["sealed"],
            "Cannot unseal original Vault",
        )
        api(
            "POST",
            "sys/mounts/secret",
            {"type": "kv", "options": {"version": "2"}},
            root_token,
            204,
        )
        baseline = {"probe": uuid4().hex, "text": "synthetic-data"}
        api("POST", "secret/data/probe", {"data": baseline}, root_token)
        api(
            "PUT",
            "sys/policies/acl/probe",
            {"policy": 'path "secret/data/probe" { capabilities = ["read"] }'},
            root_token,
            204,
        )
        api("POST", "sys/auth/approle", {"type": "approle"}, root_token, 204)
        api(
            "POST",
            "auth/approle/role/probe",
            {
                "token_policies": ["probe"],
                "token_ttl": "5m",
                "secret_id_num_uses": 0,
            },
            root_token,
            204,
        )
        role_id = api("GET", "auth/approle/role/probe/role-id", token=root_token)[
            "data"
        ]["role_id"]
        secret_id = api("POST", "auth/approle/role/probe/secret-id", {}, root_token)[
            "data"
        ]["secret_id"]
        validate(root_token, role_id, secret_id, baseline)
        docker("stop", "--time", "30", old)
        docker("network", "disconnect", network, old)
        try:
            snapshot = create_snapshot(label, "vault", volumes[0], client_id)
            volumes.append(snapshot["volume"])
            restored = restore_snapshot_to_new_volume(snapshot)
            volumes.append(restored)
        except SnapshotError as error:
            if error.volume and error.volume not in volumes:
                volumes.append(error.volume)
            raise
        new = start(new_id, restored, "new")
        new_version = wait_sealed()["version"]
        require(
            not api("PUT", "sys/unseal", {"key": key})["sealed"],
            "Cannot unseal candidate with original key",
        )
        validate(root_token, role_id, secret_id, baseline)
        updated = {"probe": uuid4().hex, "text": "candidate-write"}
        api("POST", "secret/data/probe", {"data": updated}, root_token)
        docker("restart", new)
        wait_sealed()
        api("PUT", "sys/unseal", {"key": key})
        validate(root_token, role_id, secret_id, updated)
        docker("stop", "--time", "30", new)
        docker("network", "disconnect", network, new)
        docker("network", "connect", "--alias", "vault", network, old)
        docker("start", old)
        wait_sealed()
        api("PUT", "sys/unseal", {"key": key})
        validate(root_token, role_id, secret_id, baseline)
        print(
            json.dumps(
                {
                    "old_version": old_version,
                    "new_version": new_version,
                    "secrets_preserved": True,
                    "approle_preserved": True,
                    "acl_enforced": True,
                    "restart_verified": True,
                    "original_recovered": True,
                }
            )
        )
    finally:
        failed = sys.exc_info()[0] is not None
        cleanup_errors = []
        for resource, names in (
            ("container", containers),
            ("volume", volumes),
            ("network", [network] if network else []),
        ):
            for name in reversed(names):
                result = subprocess.run(
                    [
                        "docker",
                        resource,
                        "rm",
                        *(["-f", "-v"] if resource == "container" else []),
                        name,
                    ],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode:
                    cleanup_errors.append(name)
        if cleanup_errors:
            message = "Test cleanup incomplete: " + ", ".join(cleanup_errors)
            if failed:
                print(message, file=sys.stderr)
            else:
                raise RuntimeError(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument("--client-image", required=True)
    args = parser.parse_args()
    check_upgrade(args.old_image, args.new_image, args.client_image)
