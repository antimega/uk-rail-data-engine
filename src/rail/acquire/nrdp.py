"""National Rail Data Portal feed source.

Auth is a two-step dance: POST credentials to ``/authenticate`` for a token,
then send that token in ``X-Auth-Token`` on each feed request.

Two operational constraints from the licence and the portal guidance are
enforced here rather than left to the caller:

* **Poll no more than daily.** High Volume Usage can attract charges under the
  NRE Usage Charging Document, and the published best practice is a daily
  ceiling. ``fetch`` refuses more frequent unforced polls.
* **NRDP accounts are deleted after ~30 days without feed consumption.** A poll
  counts as consumption even when the ``Last-Modified`` check means no bytes are
  downloaded, so the scheduled refresh doubles as account keep-alive.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .snapshots import SnapshotStore
from .source import Feed, FeedSource, FetchResult

BASE_URL = "https://opendata.nationalrail.co.uk"

FEED_PATHS = {
    Feed.FARES: "/api/staticfeeds/2.0/fares",
    Feed.ROUTEING: "/api/staticfeeds/2.0/routeing",
    Feed.TIMETABLE: "/api/staticfeeds/3.0/timetable",
}

MIN_POLL_INTERVAL_HOURS = 24.0
TOKEN_FILE = ".nrdp_token.json"
#: Refresh a little early rather than racing the portal's clock.
TOKEN_SAFETY_MARGIN_SECONDS = 300


class PollTooSoon(RuntimeError):
    pass


class NRDPSource(FeedSource):
    name = "nrdp"

    def __init__(
        self,
        store: SnapshotStore,
        username: str,
        password: str,
        *,
        state_dir: Path | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.store = store
        self._username = username
        self._password = password
        self._state_dir = state_dir or store.raw_dir.parent
        self._timeout = timeout

    # -- authentication ----------------------------------------------------

    @property
    def _token_path(self) -> Path:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        return self._state_dir / TOKEN_FILE

    @staticmethod
    def _token_expiry(token: str) -> datetime | None:
        # Token is "username:expiry_millis:secret"; the username may itself
        # contain no colons, so split from the right to be safe.
        parts = token.rsplit(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        return datetime.fromtimestamp(int(parts[1]) / 1000, tz=timezone.utc)

    def _cached_token(self) -> str | None:
        path = self._token_path
        if not path.exists():
            return None
        try:
            token = json.loads(path.read_text()).get("token")
        except json.JSONDecodeError:
            return None
        if not token:
            return None
        expiry = self._token_expiry(token)
        if expiry is None:
            return None
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        return token if remaining > TOKEN_SAFETY_MARGIN_SECONDS else None

    def authenticate(self, *, force: bool = False) -> str:
        if not force:
            cached = self._cached_token()
            if cached:
                return cached

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{BASE_URL}/authenticate",
                data={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"NRDP authentication failed (HTTP {response.status_code}). "
                "Check NRDP_USERNAME/NRDP_PASSWORD in .env. Note that accounts "
                "unused for ~30 days are deleted and must be re-registered."
            )

        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"NRDP authentication failed: {payload['error']}")

        token = payload.get("token")
        if not token:
            raise RuntimeError(f"NRDP returned no token: {payload}")

        roles = payload.get("roles") or {}
        if not roles.get("ROLE_DTD"):
            raise RuntimeError(
                "This account is authenticated but not subscribed to DTD "
                f"(roles: {sorted(k for k, v in roles.items() if v)}). Enable the "
                "'Fares, Routeing Guide and Timetable data' subscription on "
                f"{BASE_URL} and try again."
            )

        path = self._token_path
        path.write_text(json.dumps({"token": token}, indent=2) + "\n")
        os.chmod(path, 0o600)
        return token

    # -- fetching ----------------------------------------------------------

    def fetch(self, feed: Feed, force: bool = False) -> FetchResult:
        elapsed = self.store.hours_since_poll(feed)
        if not force and elapsed is not None and elapsed < MIN_POLL_INTERVAL_HOURS:
            raise PollTooSoon(
                f"{feed.value} was polled {elapsed:.1f}h ago; best practice is at "
                f"most once per {MIN_POLL_INTERVAL_HOURS:.0f}h. Use --force to override."
            )

        url = f"{BASE_URL}{FEED_PATHS[feed]}"
        token = self.authenticate()
        known = self.store.latest(feed)

        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            for attempt in (1, 2):
                with client.stream("GET", url, headers={"X-Auth-Token": token}) as response:
                    if response.status_code == 401 and attempt == 1:
                        # Rejected despite the local expiry check — the portal
                        # may have invalidated it. Reauthenticate and retry once.
                        response.read()
                        token = self.authenticate(force=True)
                        continue
                    return self._consume(response, feed, url, known)

        raise RuntimeError("unreachable: fetch loop exited without a result")

    def _consume(
        self,
        response: httpx.Response,
        feed: Feed,
        url: str,
        known,
    ) -> FetchResult:
        if response.status_code != 200:
            response.read()
            raise RuntimeError(
                f"NRDP returned HTTP {response.status_code} for {feed.value}: "
                f"{response.text[:300]}"
            )

        last_modified = response.headers.get("Last-Modified")
        filename = _filename_from_headers(response.headers, feed)

        # The poll itself is what keeps the account alive, so record it even
        # when we decide not to pull the body.
        self.store.record_poll(feed)

        if known and last_modified and known.last_modified == last_modified:
            response.close()
            return FetchResult(
                feed=feed,
                path=self.store.path_for(known),
                filename=known.filename,
                last_modified=last_modified,
                downloaded=False,
                reason=f"unchanged since {last_modified}",
            )

        target_dir = self.store.feed_dir(feed)
        handle = tempfile.NamedTemporaryFile(
            dir=target_dir, prefix=".partial-", suffix=".zip", delete=False
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    handle.write(chunk)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        manifest = self.store.store(
            feed,
            filename,
            temp_path,
            url=url,
            source=self.name,
            last_modified=last_modified,
        )
        return FetchResult(
            feed=feed,
            path=self.store.path_for(manifest),
            filename=manifest.filename,
            last_modified=last_modified,
            downloaded=True,
            reason=f"downloaded {manifest.size / 1e6:.1f} MB",
        )


def _filename_from_headers(headers: httpx.Headers, feed: Feed) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        return Path(match.group(1)).name
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{feed.prefix}-{stamp}.zip"
