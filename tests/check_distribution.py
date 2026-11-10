"""Check packaged public documentation, metadata and relative links without extraction."""

from __future__ import annotations

import argparse
import posixpath
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def read_archive(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name.partition("/")[2]: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }


def check(path: Path) -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    name, version = config["project"]["name"], config["project"]["version"]
    stem = name.replace("-", "_") + "-" + version
    wheel = path.suffix == ".whl"
    files = read_archive(path)
    expected = {}
    for directory, sources in config["tool"]["setuptools"]["data-files"].items():
        for source in sources:
            target = f"{stem}.data/data/{directory}/{Path(source).name}" if wheel else source
            expected[target] = ROOT / source
    if not expected:
        raise RuntimeError("No public documentation declared")
    for target, source in expected.items():
        if files.get(target) != source.read_bytes():
            raise RuntimeError(f"{path.name}: missing or stale file: {target}")
        if not target.endswith(".md"):
            continue
        for link in re.findall(r"\]\(([^)]+)\)", files[target].decode()):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = posixpath.normpath(
                posixpath.join(posixpath.dirname(target), unquote(parsed.path))
            )
            if destination not in files:
                raise RuntimeError(f"{path.name}: broken local link: {target} -> {link}")
    metadata_path = f"{stem}.dist-info/METADATA" if wheel else "PKG-INFO"
    metadata = BytesParser().parsebytes(files.get(metadata_path, b""))
    if metadata.get("Name") != name or metadata.get("Version") != version:
        raise RuntimeError(f"{path.name}: incorrect distribution name/version")
    private = {".git", ".venv", ".runtime", ".pg_stand", ".pg_play", "__pycache__", "labs"}
    internal = {"todo.md", "SPEC.md", "SPEC_REVIEW.md", "IMPLEMENTATION_STATUS.md"}
    for filename in files:
        parts = PurePosixPath(filename).parts
        if (
            private.intersection(parts)
            or parts[-1] in internal
            or parts[-1] == ".env"
            or filename.endswith((".pyc", ".log", ".pid", ".pgpass", ".key", ".pem"))
        ):
            raise RuntimeError(f"{path.name}: local/private file included: {filename}")
    print(f"{path.name}: {len(expected)} public documents/examples and local links verified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    for archive in parser.parse_args().archives:
        check(archive)


if __name__ == "__main__":
    main()
