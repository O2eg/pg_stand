from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from pg_stand import __version__
from pg_stand.assets import (
    DOCKER_FILES,
    PROFILE_FILES,
    SCHEMA_FILES,
    docker_build_context,
    initialize_project,
)
from pg_stand.errors import PgStandError

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_metadata_matches_public_package_contract() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["build-system"]["requires"] == ["setuptools>=77.0.3"]
    project = data["project"]
    assert project["name"] == "pg-stand"
    assert project["version"] == __version__
    assert project["requires-python"] == ">=3.10"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "O2eg", "email": "oleg.ispu@yandex.ru"}]
    assert project["urls"] == {
        "Homepage": "https://o2eg.com/",
        "Repository": "https://github.com/O2eg/pg_stand",
        "Issues": "https://github.com/O2eg/pg_stand/issues",
    }
    package_data = data["tool"]["setuptools"]["package-data"]["pg_stand"]
    assert ".dockerignore" in package_data
    assert "configs/*.yaml" in package_data
    assert "docker/Dockerfile.*" in package_data
    assert "docker/*.sh" in package_data
    assert "schema/*.json" in package_data
    assert data["tool"]["setuptools"]["packages"]["find"]["namespaces"] is False
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests *.py *.sh" in manifest


def test_installed_package_contains_every_runtime_asset() -> None:
    context = docker_build_context()

    assert (context / ".dockerignore").is_file()
    assert all((context / "docker" / name).is_file() for name in DOCKER_FILES)
    assert all((context / "configs" / name).is_file() for name in PROFILE_FILES)
    assert all((context / "schema" / name).is_file() for name in SCHEMA_FILES)


def test_project_initialization_is_idempotent_and_protects_local_edits(tmp_path: Path) -> None:
    first = initialize_project(tmp_path)
    second = initialize_project(tmp_path)

    assert {item["state"] for item in first["files"]} == {"created"}
    assert {item["state"] for item in second["files"]} == {"unchanged"}
    assert len(first["files"]) == len(PROFILE_FILES) + len(SCHEMA_FILES)

    profile = tmp_path / "configs" / "single.yaml"
    profile.write_text("local edit\n", encoding="utf-8")
    with pytest.raises(PgStandError, match="init --force"):
        initialize_project(tmp_path)

    forced = initialize_project(tmp_path, force=True)
    states = {Path(item["path"]).name: item["state"] for item in forced["files"]}
    assert states["single.yaml"] == "replaced"
    assert profile.read_text(encoding="utf-8").startswith("api_version: pg_stand/v1")


def test_project_initialization_refuses_symlink_targets(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "configs").symlink_to(external, target_is_directory=True)

    with pytest.raises(PgStandError, match="symbolic link"):
        initialize_project(tmp_path)


def test_project_initialization_reports_a_file_instead_of_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(PgStandError, match="cannot create project directory"):
        initialize_project(target)
