"""Bootstrap a private Vault TLS identity without private keys on the host disk."""

import ipaddress
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def issue_identity():
    """One-purpose CA. Its signing key is discarded after issuing the server cert."""
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private Vault CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vault")]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("vault"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "ca.crt": ca.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "server.crt": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "server.key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    }


def prepare_tls(project: str, image: str, directory: Path) -> Path:
    """Create once; refuse replacement or unexpected ownership of an existing volume."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError("Invalid Compose project name")
    directory = directory.resolve()
    volume = project + "_vault_tls"
    existing = subprocess.run(
        ["docker", "volume", "inspect", volume], capture_output=True, text=True
    )
    ca_file = directory / "ca.crt"
    if existing.returncode == 0:
        labels = json.loads(existing.stdout)[0].get("Labels") or {}
        if labels.get("devops.vault-tls") != project:
            raise RuntimeError("Vault TLS volume has unexpected ownership")
        # A read-only helper exposes only the public certificate, never the key.
        public = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--mount",
                f"type=volume,source={volume},target=/tls,readonly",
                "--entrypoint",
                "python",
                image,
                "-c",
                "from pathlib import Path; print(Path('/tls/ca.crt').read_text(), end='')",
            ],
            text=True,
        )
        cert = x509.load_pem_x509_certificate(public.encode("ascii"))
        if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=30):
            raise RuntimeError(
                "Vault TLS certificate expires soon; planned rotation is required"
            )
        if ca_file.exists() and ca_file.read_text(encoding="ascii") != public:
            raise RuntimeError(
                "Vault TLS trust certificate differs from the installed volume"
            )
    else:
        if ca_file.exists():
            raise RuntimeError(
                "Vault TLS volume is missing; refusing to silently replace its identity"
            )
        identity = issue_identity()
        public = identity["ca.crt"]
        subprocess.run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"devops.vault-tls={project}",
                "--label",
                f"com.docker.compose.project={project}",
                "--label",
                "com.docker.compose.volume=vault_tls",
                volume,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        writer = (
            "import json,os,sys; from pathlib import Path; values=json.load(sys.stdin); "
            "root=Path('/tls'); "
            "[( (root/n).write_text(v,encoding='ascii'), "
            "os.chmod(root/n,0o400 if n.endswith('.key') else 0o444), os.chown(root/n,100,1000)) for n,v in values.items()]"
        )
        # Private key travels on stdin into a dedicated volume. It is absent
        # from command arguments, environment, image, logs and host files.
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=volume,source={volume},target=/tls",
                "--entrypoint",
                "python",
                image,
                "-c",
                writer,
            ],
            input=json.dumps(identity),
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError(
                "Could not initialize Vault TLS volume; private output suppressed"
            )
    directory.mkdir(parents=True, exist_ok=True)
    ca_file.write_text(public, encoding="ascii")
    return ca_file
