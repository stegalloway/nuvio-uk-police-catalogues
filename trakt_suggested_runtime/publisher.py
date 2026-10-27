"""Refresh two static catalogues without committing private recommendation inputs.

Run from the repository root with ``python -m trakt_suggested_runtime``.
Only validated final catalogue metadata is written beneath docs/suggested.
Any required source failure leaves the previous published output unchanged.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid


PAGE_SIZE = 20
DEFAULT_ADDON_ID = "org.trakt.suggested.personal"
# (generator key, Stremio type, catalogue ID, name), in Home order.
CATALOGUES = (
    ("series", "series", "trakt-suggested-series", "Trakt Suggested Series"),
    ("movie", "movie", "trakt-suggested-movies", "Trakt Suggested Movies"),
    ("upcoming", "series", "trakt-upcoming-episodes", "New & Upcoming Episodes"),
)
CATALOGUE_ORDER = [key for key, *_ in CATALOGUES]
# Nothing a caught-up viewer is waiting for may air soon; that row may be empty.
MAY_BE_EMPTY = frozenset({"upcoming"})
INPUT_FILES = {
    "watched_movies": "trakt-watched-movies.json",
    "watched_shows": "trakt-watched-shows.json",
    "rated_movies": "trakt-ratings-movies.json",
    "rated_shows": "trakt-ratings-shows.json",
    "watched_episode_history": "trakt-episode-history.json",
}
OPTIONAL_INPUT_FILES = {"dismissed": "trakt-not-interested.json", "watchlist": "trakt-watchlist.json",
                        "loved": "trakt-loved.json"}
DEFAULT_DISMISS_LIST = "not-interested"
DEFAULT_LOVED_LIST = "loved"
META_FIELDS = frozenset({
    "id", "type", "name", "poster", "posterShape", "description", "releaseInfo",
    "genres", "background", "imdbRating", "runtime", "logo", "imdb_id",
})
ARTWORK_HOSTS = {"image.tmdb.org", "images.metahub.space"}


class RefreshError(RuntimeError):
    """A generation cannot safely replace the previous catalogue."""


class SourceError(RefreshError):
    """A required remote source failed or returned an incomplete response."""


class MissingSource(LookupError):
    """An optional source does not exist or is not public. Not a failure."""


class PaginationChanged(Exception):
    """A list's totals moved between pages; it is read again from page 1."""


class SourceFailures:
    """Latch failures even when recommendation code catches callback exceptions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: list[tuple[str, str]] = []

    def record(self, source: str, reason: str) -> None:
        # Source/reason are controlled labels, never request URLs or user IDs.
        with self._lock:
            self._failures.append((source, reason))

    def require_success(self) -> None:
        with self._lock:
            failed = bool(self._failures)
        if failed:
            raise SourceError("A source failed; previous catalogues must be retained")

    def labels(self) -> list[str]:
        with self._lock:
            return sorted({source + ":" + reason for source, reason in self._failures})

    def summary(self) -> dict[str, int]:
        result: dict[str, int] = {}
        with self._lock:
            for source, _ in self._failures:
                result[source] = result.get(source, 0) + 1
        return result


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    headers: Mapping[str, str]


class JsonHttpClient:
    """Bounded read-only JSON GET client with no credential-bearing error logs."""

    def __init__(
        self, base_url: str, headers: Mapping[str, str], source: str,
        failures: SourceFailures, *, extra_query: Mapping[str, str] | None = None,
        opener: Callable = urllib.request.urlopen, sleeper: Callable = time.sleep,
        deadline: float | None = None, attempts: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.source = source
        self.failures = failures
        self.extra_query = dict(extra_query or {})
        self.opener = opener
        self.sleeper = sleeper
        self.deadline = deadline
        self.attempts = attempts

    def fail(self, reason: str) -> None:
        self.failures.record(self.source, reason)
        raise SourceError(self.source + " response was unavailable or invalid")

    def get(self, path: str, params: Mapping | None = None, *, missing_ok: bool = False) -> JsonResponse:
        if not path.startswith("/") or "?" in path or "#" in path or path.startswith("//"):
            self.fail("invalid_path")
        query = {**dict(params or {}), **self.extra_query}
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, method="GET", headers={
                "Accept": "application/json", "User-Agent": "TraktSuggestedCatalogues/1.0",
                **self.headers,
            },
        )
        for attempt in range(self.attempts):
            remaining = (self.deadline - time.monotonic()) if self.deadline else 25.0
            if remaining <= 0:
                self.fail("deadline")
            retry_delay = min(2 ** attempt, 10)
            try:
                with self.opener(request, timeout=min(25.0, remaining)) as response:
                    status = getattr(response, "status", 200)
                    if status != 200:
                        self.fail("unexpected_status")
                    body = response.read(16 * 1024 * 1024 + 1)
                    if len(body) > 16 * 1024 * 1024:
                        self.fail("oversized_response")
                    try:
                        data = json.loads(body.decode("utf-8-sig"))
                    except (UnicodeError, ValueError):
                        self.fail("invalid_json")
                    if isinstance(data, dict) and data.get("success") is False:
                        self.fail("api_error_payload")
                    return JsonResponse(data, {k.lower(): v for k, v in response.headers.items()})
            except urllib.error.HTTPError as error:
                if missing_ok and error.code in (401, 403, 404):
                    raise MissingSource(self.source + " resource is absent or private")
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt + 1 == self.attempts:
                    self.fail("http_" + str(error.code))
                try:
                    retry_delay = min(10.0, max(retry_delay, float(error.headers.get("Retry-After", "0"))))
                except (TypeError, ValueError):
                    pass
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    self.fail("network")
            if self.deadline and time.monotonic() + retry_delay >= self.deadline:
                self.fail("deadline")
            self.sleeper(retry_delay)
        self.fail("exhausted")


def utc_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TmdbClient:
    """Optional private disk cache compatible with the comparison preview cache.

    List answers (recommendations, similar, discover) can also be kept in
    list_cache_dir for list_ttl_hours across runs. TMDB serves different
    versions of the same list from different CloudFront locations, and each
    hourly run lands on a different one, so a title found by one list came and
    went between runs (27 October 2026). Details are never kept there, so
    episode dates stay fresh.
    """

    LIST_PATH = re.compile(r"/(?:tv|movie)/[0-9]+/(?:recommendations|similar)|/discover/(?:tv|movie)")

    def __init__(self, http: JsonHttpClient, cache_dir: Path | None = None, ttl_hours: float = 12,
                 list_cache_dir: Path | None = None, list_ttl_hours: float = 24) -> None:
        self.http = http
        self.cache_dir = cache_dir
        self.ttl = timedelta(hours=ttl_hours)
        self.list_cache_dir = list_cache_dir
        self.list_ttl = timedelta(hours=list_ttl_hours)
        self.list_outcomes = {"kept": 0, "fetched": 0}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._memory: dict[str, dict] = {}

    def _store(self, path: str) -> tuple[Path | None, timedelta, bool]:
        """Where an answer is cached, for how long, and whether it is a kept list."""
        if self.list_cache_dir is not None and self.LIST_PATH.fullmatch(path):
            return self.list_cache_dir, self.list_ttl, True
        return self.cache_dir, self.ttl, False

    @staticmethod
    def cache_key(path: str, params: Mapping | None = None) -> str:
        # Matches the existing private cache: no API key in params.
        material = path + json.dumps(dict(params or {}), sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest() + ".json"

    def _read_cache(self, path: str, params: dict, key: str) -> dict | None:
        directory, ttl, _ = self._store(path)
        if directory is None:
            return None
        try:
            cached = json.loads((directory / key).read_text(encoding="utf-8-sig"))
            fetched = utc_datetime(cached.get("fetchedAt", ""))
            age = datetime.now(timezone.utc) - fetched if fetched else None
            if (cached.get("path") == path and cached.get("params") == params and
                    age is not None and timedelta(0) <= age <= ttl and
                    isinstance(cached.get("data"), dict) and cached["data"].get("success") is not False):
                return cached["data"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def __call__(self, path: str, params: Mapping | None = None) -> dict:
        query = dict(params or {})
        if any(k.lower() in {"api_key", "access_token", "authorization", "token"} for k in query):
            self.http.fail("credentials_in_cache_query")
        key = self.cache_key(path, query)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key in self._memory:
                return self._memory[key]
            directory, _, kept_list = self._store(path)
            value = self._read_cache(path, query, key)
            if kept_list:
                with self._guard:
                    self.list_outcomes["kept" if value is not None else "fetched"] += 1
            if value is None:
                value = self.http.get(path, query).data
                if not isinstance(value, dict):
                    self.http.fail("unexpected_payload_shape")
                if directory is not None:
                    try:
                        atomic_json(directory / key, {
                            "path": path, "params": query,
                            "fetchedAt": datetime.now(timezone.utc).isoformat(), "data": value,
                        })
                    except OSError:
                        # A cache write is optional; successful metadata may still be used.
                        pass
            self._memory[key] = value
            return value

    def prune(self) -> int:
        """Delete kept list answers past list_ttl_hours, so the kept cache stays small."""
        removed = 0
        if self.list_cache_dir is None or not self.list_cache_dir.exists():
            return removed
        now = datetime.now(timezone.utc)
        for path in self.list_cache_dir.glob("*.json"):
            try:
                fetched = utc_datetime(json.loads(path.read_text(encoding="utf-8-sig")).get("fetchedAt", ""))
            except (OSError, ValueError, AttributeError):
                fetched = None
            if fetched is None or now - fetched > self.list_ttl:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


class TraktClient:
    """Read public snapshots and fully paginated public ratings/episode history."""

    def __init__(self, http: JsonHttpClient, sleeper: Callable = time.sleep) -> None:
        self.http = http
        self.sleeper = sleeper

    def _list(self, response: JsonResponse) -> list[dict]:
        if not isinstance(response.data, list) or any(not isinstance(item, dict) for item in response.data):
            self.http.fail("unexpected_payload_shape")
        return response.data

    def snapshot(self, path: str, params: Mapping | None = None) -> list[dict]:
        # Public watched endpoints are full snapshots. Honour pagination headers
        # if a deployed API version introduces them rather than truncating.
        params = {"extended": "full", **dict(params or {})}
        first = self.http.get(path, params)
        if "x-pagination-page-count" in first.headers:
            return self.paginated(path, params)
        return self._list(first)

    def paginated(self, path: str, params: Mapping | None = None, *, limit: int = 100,
                  missing_ok: bool = False, attempts: int = 3) -> list[dict]:
        # A list that changes while it is read (a new play or rating, or a
        # cached first page) reports different totals on later pages. Read it
        # again from page 1; only totals that keep moving fail the refresh.
        for attempt in range(attempts):
            try:
                return self._paginated_once(path, params, limit, missing_ok)
            except PaginationChanged as change:
                detail = str(change)
                if attempt + 1 < attempts:
                    self.sleeper(2 * (attempt + 1))
        # The label names the endpoint without the username, plus page totals.
        self.http.fail("pagination_changed_during_fetch(" + re.sub(r"^/users/[^/]+", "", path) + ": " + detail + ")")

    def _paginated_once(self, path: str, params: Mapping | None, limit: int, missing_ok: bool) -> list[dict]:
        result: list[dict] = []
        expected_pages = expected_items = None
        fingerprints: set[str] = set()
        for page in range(1, 1001):
            # Only a missing first page means the source is absent; a later
            # page vanishing is still a failed, partial fetch.
            response = self.http.get(path, {**dict(params or {}), "limit": limit, "page": page},
                                     missing_ok=missing_ok and page == 1)
            items = self._list(response)
            headers = response.headers
            try:
                page_count = int(headers["x-pagination-page-count"]) if "x-pagination-page-count" in headers else None
                item_count = int(headers["x-pagination-item-count"]) if "x-pagination-item-count" in headers else None
                actual_page = int(headers["x-pagination-page"]) if "x-pagination-page" in headers else page
            except (ValueError, TypeError):
                self.http.fail("invalid_pagination_headers")
            if actual_page != page or (page_count is not None and page_count < 0) or (item_count is not None and item_count < 0):
                self.http.fail("invalid_pagination_headers")
            if page == 1:
                expected_pages, expected_items = page_count, item_count
            elif page_count != expected_pages or item_count != expected_items:
                raise PaginationChanged(f"page {page}, items {expected_items}->{item_count}, "
                                        f"pages {expected_pages}->{page_count}")
            if items:
                fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
                if fingerprint in fingerprints:
                    self.http.fail("repeated_pagination_page")
                fingerprints.add(fingerprint)
            result.extend(items)
            if expected_pages is not None:
                if page >= max(1, expected_pages):
                    if expected_items is not None and len(result) != expected_items:
                        self.http.fail("incomplete_pagination")
                    return result
                if not items:
                    self.http.fail("empty_nonterminal_page")
            elif len(items) < limit:
                return result
        self.http.fail("pagination_limit")

    def seasons(self, path: str, params: Mapping | None = None) -> list[dict]:
        if not re.fullmatch(r"/shows/[A-Za-z0-9._-]+/seasons", path):
            self.http.fail("unexpected_season_path")
        return self._list(self.http.get(path, params))

    def dismissed(self, username: str, slug: str, params: Mapping | None = None) -> list[dict] | None:
        """Items of the user's public "Not Interested" list; None while it is absent or private."""
        path = ("/users/" + urllib.parse.quote(username, safe="") +
                "/lists/" + urllib.parse.quote(slug, safe="") + "/items")
        try:
            return self.paginated(path, params, missing_ok=True)
        except MissingSource:
            return None

    def watchlist(self, username: str, params: Mapping | None = None) -> list[dict] | None:
        """Items of the user's public watchlist; None while it is private."""
        try:
            return self.paginated("/users/" + urllib.parse.quote(username, safe="") + "/watchlist",
                                  params, missing_ok=True)
        except MissingSource:
            return None

    def title_lookup(self, path: str, params: Mapping | None = None) -> Any:
        """A title's related titles or community rating; None when Trakt doesn't know it."""
        if not re.fullmatch(r"/(?:shows|movies)/[A-Za-z0-9._-]+/(?:related|ratings)", path):
            self.http.fail("unexpected_title_path")
        try:
            return self.http.get(path, params, missing_ok=True).data
        except MissingSource:
            return None

    def histories(self, username: str, dismiss_list: str | None = None,
                  now: datetime | None = None, loved_list: str | None = DEFAULT_LOVED_LIST,
                  ) -> dict[str, list[dict] | None]:
        user = urllib.parse.quote(username, safe="")
        base = "/users/" + user
        # A dated end gives each run's history pages their own URLs. On 19 Oct
        # Trakt served cached early pages totalling 839 plays while the last
        # page said 840. Plays after this instant are left for the next run.
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        until = moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        # Trakt's CDN keeps requests without a login for up to an hour
        # (s-maxage=3600). A per-run parameter makes every account read fresh.
        fresh = {"_": str(int(moment.timestamp()))}
        jobs = {
            "watched_movies": lambda: self.snapshot(base + "/watched/movies", fresh),
            "watched_shows": lambda: self.snapshot(base + "/watched/shows", fresh),
            "rated_movies": lambda: self.paginated(base + "/ratings/movies", fresh),
            "rated_shows": lambda: self.paginated(base + "/ratings/shows", fresh),
            "watched_episode_history": lambda: self.paginated(base + "/history/shows", {"end_at": until, **fresh}),
        }
        if dismiss_list:
            # A missing list is None; any other error fails the refresh, so an
            # outage can never bring dismissed titles back.
            jobs["dismissed"] = lambda: self.dismissed(username, dismiss_list, fresh)
        # Like the Not Interested list: private is None, an outage fails the refresh.
        jobs["watchlist"] = lambda: self.watchlist(username, fresh)
        # The "Loved" list the same way: absent or private is None.
        if loved_list:
            jobs["loved"] = lambda: self.dismissed(username, loved_list, fresh)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {key: pool.submit(job) for key, job in jobs.items()}
            # Every required future must succeed. Never interpret an outage as [].
            return {key: future.result() for key, future in futures.items()}


class MdbListClient:
    """Rotten Tomatoes critic and audience scores through MDBList, by TMDB or IMDb ID.

    Its HTTP client records failures in its own SourceFailures, so an outage or a
    used-up daily quota never fails the refresh: a title keeps its last known
    scores, or has none. Answers, missing ones included, are cached for ttl_days.
    Where MDBList has no Rotten Tomatoes score, Metacritic stands in for the
    Tomatometer and IMDb for the Popcornmeter (both 0-100 in MDBList).
    """

    MEDIA = {"series": "show", "movie": "movie"}
    CRITICS = frozenset({"tomatoes"})
    AUDIENCE = frozenset({"popcorn", "tomatoesaudience", "audience"})
    CRITICS_FALLBACK = "metacritic"
    AUDIENCE_FALLBACK = "imdb"

    def __init__(self, http: JsonHttpClient, cache_path: Path | None = None, ttl_days: float = 7) -> None:
        self.http = http
        self.cache_path = cache_path
        self.ttl = timedelta(days=ttl_days)
        self.lock = threading.Lock()
        self.sources_seen: dict[str, int] = {}
        self.outcomes: dict[str, int] = {}
        self.cache: dict[str, dict] = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}

    def _count(self, table: dict, key: str) -> None:
        with self.lock:
            table[key] = table.get(key, 0) + 1

    def _fetch(self, kind: str, tmdb_id: Any, imdb_id: Any) -> Any:
        for provider, value in (("tmdb", tmdb_id), ("imdb", imdb_id)):
            if not value or not re.fullmatch(r"[0-9]+|tt[0-9]+", str(value)):
                continue
            try:
                data = self.http.get("/" + provider + "/" + self.MEDIA[kind] + "/" + str(value), missing_ok=True).data
            except MissingSource:
                self._count(self.outcomes, provider + "_missing")
                continue
            self._count(self.outcomes, provider + "_found")
            return data
        return None

    def parse(self, data: Any) -> dict:
        ratings = data.get("ratings") if isinstance(data, Mapping) else None
        found = {"tomatometer": None, "popcornmeter": None}
        fallback = {}
        for rating in ratings if isinstance(ratings, list) else []:
            if not isinstance(rating, Mapping):
                continue
            source = str(rating.get("source") or "").lower()
            self._count(self.sources_seen, source)
            score = rating.get("score") if rating.get("score") is not None else rating.get("value")
            try:
                score = float(score)
            except (TypeError, ValueError):
                continue
            if not 0 < score <= 100:
                continue
            if source in self.CRITICS:
                found["tomatometer"] = score
            elif source in self.AUDIENCE:
                found["popcornmeter"] = score
            elif source in (self.CRITICS_FALLBACK, self.AUDIENCE_FALLBACK):
                fallback[source] = score
        found["critics_source"] = "tomatoes" if found["tomatometer"] is not None else None
        found["audience_source"] = "popcorn" if found["popcornmeter"] is not None else None
        if found["tomatometer"] is None and self.CRITICS_FALLBACK in fallback:
            found["tomatometer"], found["critics_source"] = fallback[self.CRITICS_FALLBACK], self.CRITICS_FALLBACK
        if found["popcornmeter"] is None and self.AUDIENCE_FALLBACK in fallback:
            found["popcornmeter"], found["audience_source"] = fallback[self.AUDIENCE_FALLBACK], self.AUDIENCE_FALLBACK
        return found

    def __call__(self, kind: str, tmdb_id: Any, imdb_id: Any = None) -> dict | None:
        if kind not in self.MEDIA:
            return None
        key = kind + ":" + str(tmdb_id or imdb_id)
        now = datetime.now(timezone.utc)
        with self.lock:
            cached = self.cache.get(key)
        if cached and (utc_datetime(cached.get("at", "")) or datetime.min.replace(tzinfo=timezone.utc)) > now - self.ttl:
            self._count(self.outcomes, "cached")
            return cached.get("scores")
        try:
            scores = self.parse(self._fetch(kind, tmdb_id, imdb_id))
        except SourceError:
            # The last known scores, however old, beat none: rows don't wobble.
            self._count(self.outcomes, "stale" if cached else "failed")
            return cached.get("scores") if cached else None
        with self.lock:
            self.cache[key] = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "scores": scores}
        return scores

    def save(self) -> None:
        if self.cache_path:
            with self.lock:
                atomic_json(self.cache_path, self.cache)


def listed_titles(items: Any, limit: int = 12) -> str:
    """" (A, B, ...)" naming what the public Not Interested list holds, for the run notice."""
    if not isinstance(items, list) or not items:
        return ""
    names = []
    for item in items:
        kind = item.get("type") if isinstance(item, dict) else None
        holder = (item.get(kind) if kind in ("movie", "show") else item.get("show")) if kind else None
        title = holder.get("title") if isinstance(holder, dict) else None
        names.append(" ".join(str(title).replace("%", "%25").split())[:60] if title else "?")
    more = len(names) - limit
    return " (" + ", ".join(names[:limit]) + (f", +{more} more" if more > 0 else "") + ")"


def read_snapshot(path: Path) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, dict):
        if value.get("status", 200) != 200:
            raise SourceError("A private snapshot contains a failed response")
        for key in ("data", "body", "items"):
            if key in value:
                value = value[key]
                if isinstance(value, str):
                    value = json.loads(value)
                break
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SourceError("A required private snapshot is missing or invalid")
    return value


def load_history_snapshots(folder: Path) -> dict[str, list[dict]]:
    """Explicit manual/offline option; scheduled workflow always fetches fresh data."""
    result = {name: read_snapshot(folder / filename) for name, filename in INPUT_FILES.items()}
    for name, filename in OPTIONAL_INPUT_FILES.items():
        if (folder / filename).exists():
            result[name] = read_snapshot(folder / filename)
    return result


def pagination_offsets(existing_pages: Path | None, total: int) -> set[int]:
    offsets = set(range(0, max(60, total + 2 * PAGE_SIZE), PAGE_SIZE))
    offsets.add(total)
    if existing_pages:
        for path in existing_pages.glob("skip=*.json"):
            match = re.fullmatch(r"skip=(\d+)\.json", path.name)
            if match:
                offsets.add(int(match.group(1)))
    for offset in tuple(offsets):
        offsets.update(range(offset, total, PAGE_SIZE))
    return offsets


def write_catalog_pages(folder: Path, slug: str, metas: list[dict], existing_pages: Path | None = None) -> None:
    atomic_json(folder / (slug + ".json"), {"metas": metas[:PAGE_SIZE]})
    for offset in sorted(pagination_offsets(existing_pages, len(metas))):
        atomic_json(folder / slug / f"skip={offset}.json", {"metas": metas[offset:offset + PAGE_SIZE]})


def manifest(addon_id: str = DEFAULT_ADDON_ID) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", addon_id):
        raise RefreshError("Invalid addon ID")
    return {
        "id": addon_id, "version": "1.1.0", "name": "Trakt Suggested",
        "description": "Film and series suggestions with watched filtering and returning seasons, "
                       "plus upcoming episodes of caught-up shows.",
        "types": ["series", "movie"], "resources": ["catalog"],
        "catalogs": [
            {"type": kind, "id": slug, "name": title,
             "extra": [{"name": "skip", "isRequired": False}]}
            for _, kind, slug, title in CATALOGUES
        ],
    }


def validate_meta(meta: Any, kind: str) -> None:
    if not isinstance(meta, dict) or set(meta) - META_FIELDS:
        raise RefreshError("Unexpected private or unsupported metadata fields")
    if (meta.get("type") != kind or not re.fullmatch(r"tt\d+", str(meta.get("id", ""))) or
            not isinstance(meta.get("name"), str) or not meta["name"].strip() or
            meta.get("posterShape") != "poster"):
        raise RefreshError("Invalid catalogue identity or poster shape")
    if "imdb_id" in meta and (not re.fullmatch(r"tt\d+", str(meta["imdb_id"])) or meta["imdb_id"] != meta["id"]):
        raise RefreshError("Invalid alternate IMDb identity")
    for field in ("poster", "background", "logo"):
        value = meta.get(field)
        if value is None and field != "poster":
            continue
        if not isinstance(value, str):
            raise RefreshError("Missing artwork")
        url = urllib.parse.urlsplit(value)
        if (url.scheme != "https" or url.hostname not in ARTWORK_HOSTS or url.username or url.password or
                url.query or url.fragment or not url.path or url.path == "/"):
            raise RefreshError("Artwork must use clean source URLs")
    if "genres" in meta and (not isinstance(meta["genres"], list) or any(not isinstance(g, str) for g in meta["genres"])):
        raise RefreshError("Invalid metadata genres")


def read_page(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RefreshError("Missing or invalid catalogue page") from None
    if not isinstance(value, dict) or set(value) != {"metas"} or not isinstance(value["metas"], list):
        raise RefreshError("Invalid catalogue response envelope")
    if len(value["metas"]) > PAGE_SIZE:
        raise RefreshError("Catalogue page exceeds page size")
    return value["metas"]


def validate_output(output: Path, *, minimum_items: int = 5, addon_id: str = DEFAULT_ADDON_ID) -> dict[str, int]:
    try:
        actual_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RefreshError("Missing or invalid manifest") from None
    if actual_manifest != manifest(addon_id):
        raise RefreshError("Manifest must contain exactly the ordered catalogues")
    allowed_paths = {Path("manifest.json")}
    counts = {}
    for key, kind, slug, _ in CATALOGUES:
        folder = output / "catalog" / kind
        first = read_page(folder / (slug + ".json"))
        if first != read_page(folder / slug / "skip=0.json"):
            raise RefreshError("Initial catalogue routes differ")
        collected: list[dict] = []
        offset = 0
        for _ in range(1001):
            page = read_page(folder / slug / f"skip={offset}.json")
            if not page:
                break
            collected.extend(page)
            offset += len(page)
        else:
            raise RefreshError("Catalogue pagination did not terminate")
        if len(collected) < (0 if key in MAY_BE_EMPTY else minimum_items):
            raise RefreshError("Catalogue has too few useful titles")
        identities = set()
        for item in collected:
            validate_meta(item, kind)
            if item["id"] in identities:
                raise RefreshError("Duplicate catalogue identity")
            identities.add(item["id"])
        for path in (folder / slug).glob("skip=*.json"):
            match = re.fullmatch(r"skip=(\d+)\.json", path.name)
            if not match:
                raise RefreshError("Invalid pagination filename")
            skip = int(match.group(1))
            if read_page(path) != collected[skip:skip + PAGE_SIZE]:
                raise RefreshError("Stale cached pagination route")
            allowed_paths.add(path.relative_to(output))
        allowed_paths.add((folder / (slug + ".json")).relative_to(output))
        counts[key] = len(collected)
    actual_paths = {p.relative_to(output) for p in output.rglob("*") if p.is_file()}
    if actual_paths != allowed_paths or any(p.is_symlink() for p in output.rglob("*")):
        raise RefreshError("Unexpected file or link in public output")
    return counts


def promote_output(stage: Path, output: Path) -> None:
    """Stage a complete validated directory, with rollback if promotion fails.

    Git publication is one commit after every row validates. The local directory
    swap never exposes partially generated pages and is rollback-safe on Windows.
    """
    backup = output.with_name("." + output.name + ".previous." + uuid.uuid4().hex)
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise RefreshError("Output must be an owned directory")
    had_output = output.exists()
    if had_output:
        os.replace(output, backup)
    try:
        os.replace(stage, output)
    except BaseException:
        if had_output:
            os.replace(backup, output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def publish_result(result: Mapping, output: Path, *, minimum_items: int = 5,
                   addon_id: str = DEFAULT_ADDON_ID) -> dict[str, int]:
    if result.get("catalogue_order") != CATALOGUE_ORDER or set(result.get("catalogues", {})) != set(CATALOGUE_ORDER):
        raise RefreshError("Generator did not return exactly the ordered catalogues")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + output.name + ".stage.", dir=output.parent) as temporary:
        stage = Path(temporary) / "output"
        stage.mkdir()
        atomic_json(stage / "manifest.json", manifest(addon_id))
        for key, kind, slug, title in CATALOGUES:
            catalogue = result["catalogues"][key]
            payload = catalogue.get("payload")
            if (catalogue.get("id") != slug or catalogue.get("name") != title or catalogue.get("type") != kind or
                    not isinstance(payload, dict) or set(payload) != {"metas"} or not isinstance(payload["metas"], list)):
                raise RefreshError("Generator catalogue does not match the public contract")
            metas = payload["metas"]
            for meta in metas:
                validate_meta(meta, kind)
            write_catalog_pages(stage / "catalog" / kind, slug, metas,
                                existing_pages=output / "catalog" / kind / slug)
        counts = validate_output(stage, minimum_items=minimum_items, addon_id=addon_id)
        promote_output(stage, output)
    return counts


def generate_and_publish(
    histories: Mapping[str, list[dict]], tmdb_fetch: Callable, trakt_fetch: Callable,
    output: Path, *, failures: SourceFailures, generator: Callable | None = None,
    now: datetime | None = None, addon_id: str = DEFAULT_ADDON_ID,
    private_diagnostics: Path | None = None, profile: str | None = None,
    trakt_lookup: Callable | None = None, rt_lookup: Callable | None = None,
) -> dict[str, int]:
    if generator is None:
        from trakt_suggested_engine import generate_catalogues
        generator = generate_catalogues
    if profile is not None:
        from trakt_suggested_engine import PROFILES, EngineConfig
        if profile not in PROFILES:
            raise RefreshError("Unknown taste profile")
    failures.require_success()
    for name in INPUT_FILES:
        if name not in histories or not isinstance(histories[name], list):
            raise SourceError("A required history list is missing")
    # The Not Interested list is optional: None (absent or private) dismisses nothing.
    extra = {"dismissed": histories["dismissed"]} if histories.get("dismissed") is not None else {}
    if extra and not isinstance(extra["dismissed"], list):
        raise SourceError("The Not Interested list is invalid")
    # The watchlist is optional the same way: None (private) applies no watchlist rules.
    if histories.get("watchlist") is not None:
        if not isinstance(histories["watchlist"], list):
            raise SourceError("The watchlist is invalid")
        extra["watchlist"] = histories["watchlist"]
    # And the Loved list: None (absent or private) loves nothing.
    if histories.get("loved") is not None:
        if not isinstance(histories["loved"], list):
            raise SourceError("The Loved list is invalid")
        extra["loved"] = histories["loved"]
    if trakt_lookup is not None:
        extra["trakt_lookup"] = trakt_lookup
    if rt_lookup is not None:
        extra["rt_lookup"] = rt_lookup
    if profile is not None:
        extra["config"] = EngineConfig(profile=profile)
    result = generator(
        histories["watched_movies"], histories["watched_shows"],
        histories["rated_movies"], histories["rated_shows"], histories["watched_episode_history"],
        tmdb_fetch, trakt_fetch=trakt_fetch, now=now, **extra,
    )
    # Engines may deliberately catch individual callback failures. A live
    # publication must still retain its last complete good result in that case.
    failures.require_success()
    if private_diagnostics:
        public_root = public_tree_root(output)
        destination = private_diagnostics.resolve()
        if destination == public_root or public_root in destination.parents:
            raise RefreshError("Private diagnostics cannot be placed in public output")
        atomic_json(private_diagnostics, result.get("diagnostics", {}))
    return publish_result(result, output, addon_id=addon_id)


def public_tree_root(output: Path) -> Path:
    """Protect all Pages files, including paths staged by the police workflow."""
    resolved = output.resolve()
    return next((p for p in (resolved, *resolved.parents) if p.name == "docs"), resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/suggested"))
    parser.add_argument("--addon-id", default=DEFAULT_ADDON_ID)
    parser.add_argument("--cache-dir", type=Path, help="Optional private TMDB cache; never committed")
    parser.add_argument("--cache-ttl-hours", type=float, default=12)
    parser.add_argument("--history-dir", type=Path, help="Explicit private offline snapshot directory; manual runs only")
    parser.add_argument("--private-diagnostics", type=Path, help="Optional local diagnostics, outside the public output")
    parser.add_argument("--timeout-minutes", type=float, default=25)
    parser.add_argument("--tmdb-list-cache", type=Path,
                        help="Optional private folder of TMDB list answers kept between runs; never committed")
    parser.add_argument("--mdblist-cache", type=Path,
                        help="Optional private MDBList answer cache, kept between runs; never committed")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    failures = SourceFailures()
    # Workflow commands become annotations, which GitHub shows without sign-in.
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        if args.validate_only:
            counts = validate_output(args.output, addon_id=args.addon_id)
            print(json.dumps({"validated": True, "counts": counts}))
            return 0
        if args.timeout_minutes <= 0 or args.cache_ttl_hours < 0:
            raise RefreshError("Invalid timeout or cache lifetime")
        username = os.environ.get("TRAKT_USERNAME", "").strip()
        trakt_key = os.environ.get("TRAKT_CLIENT_ID", "").strip()
        tmdb_token = os.environ.get("TMDB_ACCESS_TOKEN", "").strip()
        tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
        if not trakt_key or not (tmdb_token or tmdb_key) or (not args.history_dir and not username):
            raise RefreshError("Required API configuration is missing")
        public_root = public_tree_root(args.output)
        for private_path in (args.cache_dir, args.history_dir, args.private_diagnostics, args.mdblist_cache,
                             args.tmdb_list_cache):
            if private_path and (private_path.resolve() == public_root or
                                 public_root in private_path.resolve().parents):
                raise RefreshError("Private files cannot be stored in public output")
        deadline = time.monotonic() + args.timeout_minutes * 60
        trakt = TraktClient(JsonHttpClient(
            "https://api.trakt.tv", {"trakt-api-version": "2", "trakt-api-key": trakt_key},
            "trakt", failures, deadline=deadline,
        ))
        tmdb = TmdbClient(JsonHttpClient(
            "https://api.themoviedb.org/3", {"Authorization": "Bearer " + tmdb_token} if tmdb_token else {},
            "tmdb", failures, extra_query={} if tmdb_token else {"api_key": tmdb_key}, deadline=deadline,
        ), args.cache_dir, args.cache_ttl_hours, list_cache_dir=args.tmdb_list_cache)
        dismiss_list = os.environ.get("TRAKT_DISMISS_LIST", DEFAULT_DISMISS_LIST).strip()
        # Unset means the engine's default profile ("ste").
        profile = os.environ.get("TRAKT_SUGGESTED_PROFILE", "").strip() or None
        loved_list = os.environ.get("TRAKT_LOVED_LIST", DEFAULT_LOVED_LIST).strip()
        # Rotten Tomatoes through MDBList, only when its key is set. Its failures
        # stay in their own log: they never stop a refresh.
        mdblist_key = os.environ.get("MDBLIST_API_KEY", "").strip()
        rt_failures = SourceFailures()
        mdblist = (MdbListClient(JsonHttpClient("https://api.mdblist.com", {}, "mdblist", rt_failures,
                                                extra_query={"apikey": mdblist_key}, deadline=deadline, attempts=2),
                                 args.mdblist_cache) if mdblist_key else None)
        histories = (load_history_snapshots(args.history_dir) if args.history_dir
                     else trakt.histories(username, dismiss_list, loved_list=loved_list))
        try:
            counts = generate_and_publish(
                histories, tmdb, trakt.seasons, args.output, failures=failures,
                now=datetime.now(timezone.utc), addon_id=args.addon_id, private_diagnostics=args.private_diagnostics,
                profile=profile, trakt_lookup=trakt.title_lookup, rt_lookup=mdblist,
            )
        finally:
            if mdblist:
                mdblist.save()
            tmdb.prune()
        rotten_tomatoes = ({**dict(sorted(mdblist.outcomes.items())), "errors": rt_failures.labels()}
                           if mdblist else "not used")
        dismissed = histories.get("dismissed")
        not_interested = (len(dismissed) if isinstance(dismissed, list) else
                          "no public list named " + dismiss_list if dismiss_list and not args.history_dir
                          else "not used")
        loved = histories.get("loved")
        loved_count = (len(loved) if isinstance(loved, list) else
                       "no public list named " + loved_list if loved_list and not args.history_dir
                       else "not used")
        tmdb_lists = dict(tmdb.list_outcomes) if args.tmdb_list_cache else "not kept"
        print(json.dumps({"published": True, "counts": counts, "not_interested": not_interested,
                          "loved": loved_count, "rotten_tomatoes": rotten_tomatoes, "tmdb_lists": tmdb_lists}))
        if in_actions:
            print("::notice title=Trakt Suggested published::series " + str(counts.get("series")) +
                  ", movie " + str(counts.get("movie")) + ", upcoming " + str(counts.get("upcoming")) +
                  ", not interested " + str(not_interested) + listed_titles(dismissed) +
                  ", loved " + str(loved_count) + ", rotten tomatoes " +
                  (", ".join(k + " " + str(v) for k, v in rotten_tomatoes.items() if k != "errors")
                   + (" (" + ", ".join(rotten_tomatoes["errors"]) + ")" if rotten_tomatoes["errors"] else "")
                   if isinstance(rotten_tomatoes, dict) else rotten_tomatoes) +
                  ", tmdb lists " + (", ".join(k + " " + str(v) for k, v in tmdb_lists.items())
                                     if isinstance(tmdb_lists, dict) else tmdb_lists))
        return 0
    except Exception as error:
        # Errors can carry credential-bearing request URLs or private titles.
        # Report only the exception class, the controlled source:reason labels
        # and the engine's fixed messages; never diagnostics, URLs or traces.
        detail = type(error).__name__
        if detail == "GenerationError" and error.args and isinstance(error.args[0], str):
            detail += ": " + error.args[0]
        labels = failures.labels()
        if labels:
            detail += " [" + ", ".join(labels) + "]"
        print("Refresh aborted (" + detail + "); no new catalogue generation was published.", file=sys.stderr)
        if in_actions:
            print("::error title=Trakt Suggested refresh aborted::" + detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
