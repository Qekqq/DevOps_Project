"""Model files travel through authenticated DVC, never image/release artifacts."""

import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath


def inventory(folder):
    folder = Path(folder).resolve(strict=True)
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("Model symlinks are not allowed")
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if "current.json" not in result:
        raise ValueError("Model release has no current.json")
    return result


def stage_models(manifest, source, destination):
    expected = manifest.get("model_files")
    if expected is None:
        return  # Support previously installed releases with models inside the image.
    if not isinstance(expected, dict) or not expected or "current.json" not in expected:
        raise ValueError("Empty model inventory")
    for name in expected:
        if not isinstance(name, str):
            raise ValueError("Invalid model path")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("Invalid model path")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
        for value in expected.values()
    ):
        raise ValueError("Invalid model checksum")
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if inventory(source) != expected:
        raise ValueError("Downloaded models differ from the models verified by CI")
    if source != destination:
        if destination.exists():
            if inventory(destination) != expected:
                raise ValueError("Installed model directory was modified")
        else:
            shutil.copytree(source, destination)
    if inventory(destination) != expected:
        raise ValueError("Model copy failed verification")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path("models"))
    args = parser.parse_args()
    manifest = json.loads((args.release / "release.json").read_text(encoding="utf-8"))
    stage_models(manifest, args.source, args.release / "models")
