from pathlib import Path

import tomli

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

with _PYPROJECT_PATH.open("rb") as _f:
    VERSION = tomli.load(_f)["tool"]["poetry"]["version"]
