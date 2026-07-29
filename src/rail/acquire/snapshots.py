"""Immutable, checksummed snapshot store for downloaded feed ZIPs.

Every downstream result is traceable to the exact input that produced it, so
snapshots are never overwritten or mutated in place. A snapshot is a ZIP plus a
sidecar manifest holding provenance.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .source import Feed

MANIFEST_SUFFIX = ".manifest.json"
POLL_FILE = "_poll.json"


@dataclass(frozen=True)
class Manifest:
    feed: str
    filename: str
    url: str
    source: str
    fetched_at: str
    last_modified: str | None
    size: int
    sha256: str
    #: Sequence number parsed from the filename (e.g. RJFAF0123 -> 123), which
    #: is how RSP versions a feed. None if the name doesn't carry one.
    sequence: int | None

    @property
    def zip_name(self) -> str:
        return self.filename


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sequence(filename: str) -> int | None:
    stem = Path(filename).stem
    trailing = ""
    for char in reversed(stem):
        if char.isdigit():
            trailing = char + trailing
        else:
            break
    return int(trailing) if trailing else None


class SnapshotStore:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    def feed_dir(self, feed: Feed) -> Path:
        path = self.raw_dir / feed.value
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- reading -----------------------------------------------------------

    def manifests(self, feed: Feed) -> list[Manifest]:
        found = []
        for path in sorted(self.feed_dir(feed).glob(f"*{MANIFEST_SUFFIX}")):
            found.append(Manifest(**json.loads(path.read_text())))
        found.sort(key=lambda m: (m.sequence or -1, m.fetched_at))
        return found

    def latest(self, feed: Feed) -> Manifest | None:
        found = self.manifests(feed)
        return found[-1] if found else None

    def path_for(self, manifest: Manifest) -> Path:
        return self.feed_dir(Feed(manifest.feed)) / manifest.filename

    # -- writing -----------------------------------------------------------

    def store(
        self,
        feed: Feed,
        filename: str,
        temp_path: Path,
        *,
        url: str,
        source: str,
        last_modified: str | None,
    ) -> Manifest:
        target_dir = self.feed_dir(feed)
        digest = sha256_file(temp_path)
        target = target_dir / filename

        if target.exists():
            # Same name already held. Identical content is a no-op; differing
            # content means RSP reissued under a reused name, so keep both.
            if sha256_file(target) == digest:
                temp_path.unlink(missing_ok=True)
            else:
                target = target_dir / f"{Path(filename).stem}-{digest[:8]}{Path(filename).suffix}"
                shutil.move(str(temp_path), target)
        else:
            shutil.move(str(temp_path), target)

        manifest = Manifest(
            feed=feed.value,
            filename=target.name,
            url=url,
            source=source,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_modified=last_modified,
            size=target.stat().st_size,
            sha256=digest,
            sequence=parse_sequence(target.name),
        )
        (target_dir / f"{target.name}{MANIFEST_SUFFIX}").write_text(
            json.dumps(asdict(manifest), indent=2) + "\n"
        )
        return manifest

    # -- poll bookkeeping --------------------------------------------------

    def last_poll(self, feed: Feed) -> datetime | None:
        path = self.feed_dir(feed) / POLL_FILE
        if not path.exists():
            return None
        stamp = json.loads(path.read_text()).get("last_polled_at")
        return datetime.fromisoformat(stamp) if stamp else None

    def record_poll(self, feed: Feed) -> None:
        path = self.feed_dir(feed) / POLL_FILE
        path.write_text(
            json.dumps(
                {"last_polled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                indent=2,
            )
            + "\n"
        )

    def hours_since_poll(self, feed: Feed) -> float | None:
        last = self.last_poll(feed)
        if last is None:
            return None
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600
