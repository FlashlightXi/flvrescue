from __future__ import annotations

import tomllib
from pathlib import Path

from flvrescue import __version__


def test_package_and_project_versions_match() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert __version__ == "0.3.0"
    assert project["project"]["version"] == __version__
