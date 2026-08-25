"""Project paths and credentials.

Credentials come from a git-ignored ``.env``. The DTD licence makes portal
access personal to the licensee and non-assignable, so they stay local and are
never written into snapshots, logs or manifests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

def _project_root() -> Path:
    """The checkout this is running from, or the working directory.

    ``parents[2]`` is right for an editable install - ``src/rail/config.py`` up
    to the repo root - and wrong for a wheel in ``site-packages``, where it
    lands somewhere in the environment. Anything importing this as an ordinary
    dependency is in the second case, so the walk has to be checked rather than
    counted: look for a marker, and fall back to the working directory.

    ``RAIL_DATA_DIR`` overrides the whole question and is the supported way to
    point several checkouts at one copy of the data, which is 3 GB of feeds and
    should not be duplicated.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return Path.cwd()


PROJECT_ROOT = _project_root()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    nrdp_username: str | None
    nrdp_password: str | None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rail.duckdb"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def require_credentials(self) -> tuple[str, str]:
        if not self.nrdp_username or not self.nrdp_password:
            raise RuntimeError(
                "NRDP credentials missing. Copy .env.example to .env and set "
                "NRDP_USERNAME and NRDP_PASSWORD (register at "
                "https://opendata.nationalrail.co.uk)."
            )
        return self.nrdp_username, self.nrdp_password


def load_config() -> Config:
    # The working directory first, so a project consuming this as a dependency
    # keeps its own credentials rather than reaching into the engine checkout.
    # `load_dotenv` does not overwrite what is already set, so the first file
    # to define a name wins and the environment beats both.
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(os.environ.get("RAIL_DATA_DIR") or (PROJECT_ROOT / "data"))
    return Config(
        data_dir=data_dir,
        nrdp_username=os.environ.get("NRDP_USERNAME") or None,
        nrdp_password=os.environ.get("NRDP_PASSWORD") or None,
    )
