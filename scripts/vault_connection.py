"""Connect to the installed Vault without proxies, redirects or TLS downgrades."""

import os
import re
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CLIENTS = ("diabetes-api", "kafka-consumer", "metrics-exporter")


def host_bind_source(source, *, windows=None):
    """Docker Desktop inspect uses Linux VM paths for Windows drive bindings."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        match = re.fullmatch(
            r"/(?:run/desktop/mnt/host|host_mnt)/([a-zA-Z])/(.*)", source
        )
        if match:
            return match[1].upper() + ":/" + match[2]
    return source


def environment(container):
    return dict(
        item.split("=", 1) for item in container["Config"]["Env"] if "=" in item
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class VaultConnection:
    url: str
    opener: urllib.request.OpenerDirector


def installed_connection(services):
    settings = [environment(services[name]) for name in CLIENTS]
    addresses = {item.get("VAULT_ADDR") for item in settings}
    if len(addresses) != 1:
        raise RuntimeError(
            "Vault client addresses disagree; migration must be completed"
        )
    address = urlsplit(addresses.pop() or "")
    if (
        address.scheme not in ("http", "https")
        or address.hostname != "vault"
        or address.port != 8200
        or address.username
        or address.password
        or address.query
        or address.fragment
        or address.path not in ("", "/")
    ):
        raise RuntimeError("Unexpected installed Vault address")
    bindings = (
        services["vault"]["HostConfig"].get("PortBindings", {}).get("8200/tcp") or []
    )
    ports = {item["HostPort"] for item in bindings if item.get("HostIp") == "127.0.0.1"}
    if len(ports) != 1:
        raise RuntimeError("Vault requires an explicit loopback port binding")
    port = ports.pop()
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Invalid Vault loopback port")
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if address.scheme == "https":
        sources = set()
        for name, env in zip(CLIENTS, settings):
            target = env.get("VAULT_CACERT")
            mounts = [
                m
                for m in services[name].get("Mounts", [])
                if m["Destination"] == target
            ]
            if (
                not target
                or len(mounts) != 1
                or mounts[0]["Type"] != "bind"
                or mounts[0]["RW"]
            ):
                raise RuntimeError(
                    "Vault requires a read-only trusted CA mount for every client"
                )
            sources.add(
                str(Path(host_bind_source(mounts[0]["Source"])).resolve(strict=True))
            )
        if len(sources) != 1:
            raise RuntimeError("Vault clients trust different CA files")
        context = ssl.create_default_context(cafile=sources.pop())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        handlers.append(urllib.request.HTTPSHandler(context=context))
    elif any(item.get("VAULT_CACERT") for item in settings):
        raise RuntimeError("Refusing HTTP Vault with TLS trust settings")
    return VaultConnection(
        f"{address.scheme}://127.0.0.1:{port}/v1/",
        urllib.request.build_opener(*handlers),
    )


def preserve_transport(config, services):
    """Carry installed trust settings into application updates without copying keys."""
    for name in CLIENTS:
        source = environment(services[name])
        target = config["services"][name].setdefault("environment", {})
        for key in (
            "VAULT_ADDR",
            "VAULT_KV_MOUNT",
            "VAULT_DB_SECRET_PATH",
            "VAULT_KAFKA_SECRET_PATH",
        ):
            if key in source:
                target[key] = source[key]
        target.pop("VAULT_CACERT", None)
        ca = source.get("VAULT_CACERT")
        if ca:
            mounts = [
                m for m in services[name].get("Mounts", []) if m["Destination"] == ca
            ]
            if len(mounts) != 1 or mounts[0]["Type"] != "bind" or mounts[0]["RW"]:
                raise RuntimeError("Installed Vault CA mount is not read-only")
            target["VAULT_CACERT"] = ca
            entries = config["services"][name].setdefault("volumes", [])
            entries[:] = [m for m in entries if m["target"] != ca]
            entries.append(
                {
                    "type": "bind",
                    "source": host_bind_source(mounts[0]["Source"]),
                    "target": ca,
                    "read_only": True,
                }
            )
    if "vault" in config["services"] and environment(services["diabetes-api"]).get(
        "VAULT_CACERT"
    ):
        vault = config["services"]["vault"]
        mounts = {m["Destination"]: m for m in services["vault"].get("Mounts", [])}
        tls, server = mounts.get("/vault/tls"), mounts.get("/vault/config/server.hcl")
        if (
            not tls
            or tls["Type"] != "volume"
            or tls["RW"]
            or not server
            or server["Type"] != "bind"
            or server["RW"]
        ):
            raise RuntimeError(
                "Installed Vault TLS server mounts are incomplete or writable"
            )
        config.setdefault("volumes", {})["vault_tls"] = {
            "name": tls["Name"],
            "external": True,
        }
        entries = vault.setdefault("volumes", [])
        entries[:] = [
            m
            for m in entries
            if m["target"] not in ("/vault/tls", "/vault/config/server.hcl")
        ]
        entries += [
            {
                "type": "volume",
                "source": "vault_tls",
                "target": "/vault/tls",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": host_bind_source(server["Source"]),
                "target": "/vault/config/server.hcl",
                "read_only": True,
            },
        ]
        vault.setdefault("healthcheck", {})["test"] = services["vault"]["Config"][
            "Healthcheck"
        ]["Test"]
