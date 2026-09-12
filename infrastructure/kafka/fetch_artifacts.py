"""Download only the reviewed, hash-pinned Maven artifacts in this build context."""

import hashlib
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def fetch_artifacts(lock_file, output):
    records = json.loads(Path(lock_file).read_text(encoding="utf-8"))
    output = Path(output)
    output.mkdir()
    seen = set()
    for record in records:
        name, url, expected = record["file"], record["url"], record["sha512"]
        parsed = urlsplit(url)
        if (
            not re.fullmatch(r"[a-z0-9-]+-[0-9.]+\.jar", name)
            or name in seen
            or parsed.scheme != "https"
            or parsed.netloc != "repo.maven.apache.org"
            or not parsed.path.endswith("/" + name)
            or not re.fullmatch(r"[a-f0-9]{128}", expected)
        ):
            raise ValueError("Invalid artifact lock entry")
        seen.add(name)
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(32 * 1024 * 1024)
        if hashlib.sha512(data).hexdigest() != expected:
            raise ValueError("Artifact checksum mismatch: " + name)
        (output / name).write_bytes(data)


if __name__ == "__main__":
    fetch_artifacts("artifacts.lock.json", "/patches")
