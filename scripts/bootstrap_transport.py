"""Private stdin transport used only during the first VPS installation."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

RPC = """
import base64,json,os,subprocess,sys
p=json.load(sys.stdin)
r=subprocess.run(p['argv'],input=base64.b64decode(p['input']),env=dict(os.environ,**p['env']),capture_output=True)
json.dump({'code':r.returncode,'output':base64.b64encode(r.stdout).decode()},sys.stdout)
"""


class Transport:
    def __init__(self, ssh=None):
        self.ssh = ssh

    def run(self, argv, *, data=b"", env=None):
        if self.ssh:
            import shlex

            payload = json.dumps(
                {
                    "argv": argv,
                    "input": base64.b64encode(data).decode(),
                    "env": env or {},
                }
            ).encode()
            result = subprocess.run(
                [*self.ssh, "python3 -c " + shlex.quote(RPC)],
                input=payload,
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError(
                    "SSH operation failed; private diagnostics suppressed"
                )
            reply = json.loads(result.stdout)
            code, output = reply["code"], base64.b64decode(reply["output"])
        else:
            result = subprocess.run(
                argv,
                input=data,
                env=dict(os.environ, **(env or {})),
                capture_output=True,
            )
            code, output = result.returncode, result.stdout
        if code:
            raise RuntimeError(
                f"Bootstrap command failed ({argv[0]}, exit {code}); private diagnostics suppressed"
            )
        return output

    def python(self, program, *, payload=None):
        import sys

        return self.run(
            ["python3" if self.ssh else sys.executable, "-c", program],
            data=json.dumps(payload).encode(),
        )

    def write(self, path, content, *, exclusive=True, mode=0o644):
        self.python(
            "import base64,hashlib,json,os,sys; from pathlib import Path; p=json.load(sys.stdin); "
            "f=Path(p['path']); f.parent.mkdir(parents=True,exist_ok=True); "
            "fd=os.open(f,os.O_WRONLY|os.O_CREAT|(os.O_EXCL if p['exclusive'] else os.O_TRUNC),p['mode']); "
            "s=os.fdopen(fd,'wb'); s.write(base64.b64decode(p['content'])); s.flush(); os.fsync(s.fileno()); s.close(); "
            "assert hashlib.sha256(f.read_bytes()).hexdigest()==p['sha256'], 'File verification failed'",
            payload={
                "path": str(path),
                "content": base64.b64encode(content).decode(),
                "exclusive": exclusive,
                "mode": mode,
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    def docker(self, *args, data=b"", env=None):
        return self.run(["docker", *args], data=data, env=env)


def ssh_connection(host, user, key):
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Invalid VPS host")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Invalid VPS user")
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-i",
        str(Path(key).resolve(strict=True)),
        user + "@" + host,
    ]
