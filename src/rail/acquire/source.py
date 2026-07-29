"""Feed source abstraction.

RDG plans to retire the National Rail Data Portal in favour of the Rail Data
Marketplace. Everything downstream of this module works from snapshots on disk,
so migrating means writing one new :class:`FeedSource` implementation and
changing nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class Feed(str, Enum):
    TIMETABLE = "timetable"
    FARES = "fares"
    ROUTEING = "routeing"

    @property
    def prefix(self) -> str:
        """Filename prefix RSP uses for this feed's ZIPs."""
        return {"timetable": "RJTTF", "fares": "RJFAF", "routeing": "RJRG"}[self.value]


@dataclass(frozen=True)
class FetchResult:
    feed: Feed
    #: Path to the stored ZIP. Set even when ``downloaded`` is False (the
    #: existing snapshot that made the download unnecessary).
    path: Path | None
    filename: str | None
    last_modified: str | None
    downloaded: bool
    reason: str


class FeedSource(Protocol):
    """Retrieves feed ZIPs into the snapshot store."""

    name: str

    def fetch(self, feed: Feed, force: bool = False) -> FetchResult: ...
