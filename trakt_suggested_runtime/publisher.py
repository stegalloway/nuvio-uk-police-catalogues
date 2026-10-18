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
CATALOGUES = (
    ("series", "trakt-suggested-series", "Trakt Suggested Series"),
    ("movie", "trakt-suggested-movies", "Trakt Suggested Movies"),
)
INPUT_FILES = {
    "watched_movies": "trakt-watched-movies.json",
    "watched_shows": "trakt-watched-shows.json",
    "rated_movies": "trakt-ratings-movies.json",
    "rated_shows": "trakt-ratings-shows.json",
    "watched_episode_history": "trakt-episode-history.json",
}
META_FIELDS = frozenset({
    "id", "type", "name", "poster", "posterShape", "description", "releaseInfo",
    "genres", "background", "imdbRating", "runtime", "logo", "imdb_id",
})
ARTWORK_HOSTS = {"image.tmdb.org", "images.metahub.space"}


class RefreshError(RuntimeError):
    """A generation cannot safely replace the previous catalogue."""


class SourceError(RefreshError):
    """A required remote source failed or returned an incomplete response."""


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

    def get(self, path: str, params: Mapping | None = None) -> JsonResponse:
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
    """Optional private disk cache compatible with the comparison preview cache."""

    def __init__(self, http: JsonHttpClient, cache_dir: Path | None = None, ttl_hours: float = 12) -> None:
        self.http = http
        self.cache_dir = cache_dir
        self.ttl = timedelta(hours=ttl_hours)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._memory: dict[str, dict] = {}

    @staticmethod
    def cache_key(path: str, params: Mapping | None = None) -> str:
        # Matches the existing private cache: no API key in params.
        material = path + json.dumps(dict(params or {}), sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest() + ".json"

    def _read_cache(self, path: str, params: dict, key: str) -> dict | None:
        if self.cache_dir is None:
            return None
        try:
            cached = json.loads((self.cache_dir / key).read_text(encoding="utf-8-sig"))
            fetched = utc_datetime(cached.get("fetchedAt", ""))
            age = datetime.now(timezone.utc) - fetched if fetched else None
            if (cached.get("path") == path and cached.get("params") == params and
                    age is not None and timedelta(0) <= age <= self.ttl and
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
            value = self._read_cache(path, query, key)
            if value is None:
                value = self.http.get(path, query).data
                if not isinstance(value, dict):
                    self.http.fail("unexpected_payload_shape")
                if self.cache_dir is not None:
                    try:
                        atomic_json(self.cache_dir / key, {
                            "path": path, "params": query,
                            "fetchedAt": datetime.now(timezone.utc).isoformat(), "data": value,
                        })
                    except OSError:
                        # A cache write is optional; successful metadata may still be used.
                        pass
            self._memory[key] = value
            return value


class TraktClient:
    """Read public snapshots and fully paginated public ratings/episode history."""

    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    def _list(self, response: JsonResponse) -> list[dict]:
        if not isinstance(response.data, list) or any(not isinstance(item, dict) for item in response.data):
            self.http.fail("unexpected_payload_shape")
        return response.data

    def snapshot(self, path: str) -> list[dict]:
        # Public watched endpoints are full snapshots. Honour pagination headers
        # if a deployed API version introduces them rather than truncating.
        first = self.http.get(path, {"extended": "full"})
        if "x-pagination-page-count" in first.headers:
            return self.paginated(path, {"extended": "full"})
        return self._list(first)

    def paginated(self, path: str, params: Mapping | None = None, *, limit: int = 100) -> list[dict]:
        result: list[dict] = []
        expected_pages = expected_items = None
        fingerprints: set[str] = set()
        for page in range(1, 1001):
            response = self.http.get(path, {**dict(params or {}), "limit": limit, "page": page})
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
                self.http.fail("pagination_changed_during_fetch")
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

    def histories(self, username: str) -> dict[str, list[dict]]:
        user = urllib.parse.quote(username, safe="")
        base = "/users/" + user
        jobs = {
            "watched_movies": lambda: self.snapshot(base + "/watched/movies"),
            "watched_shows": lambda: self.snapshot(base + "/watched/shows"),
            "rated_movies": lambda: self.paginated(base + "/ratings/movies"),
            "rated_shows": lambda: self.paginated(base + "/ratings/shows"),
            "watched_episode_history": lambda: self.paginated(base + "/history/shows"),
        }
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {key: pool.submit(job) for key, job in jobs.items()}
            # Every required future must succeed. Never interpret an outage as [].
            return {key: future.result() for key, future in futures.items()}


def load_history_snapshots(folder: Path) -> dict[str, list[dict]]:
    """Explicit manual/offline option; scheduled workflow always fetches fresh data."""
    result = {}
    for name, filename in INPUT_FILES.items():
        value = json.loads((folder / filename).read_text(encoding="utf-8-sig"))
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
        result[name] = value
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
        "id": addon_id, "version": "1.0.0", "name": "Trakt Suggested",
        "description": "Film and series suggestions with watched filtering and returning seasons.",
        "types": ["series", "movie"], "resources": ["catalog"],
        "catalogs": [
            {"type": kind, "id": slug, "name": title,
             "extra": [{"name": "skip", "isRequired": False}]}
            for kind, slug, title in CATALOGUES
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
        raise RefreshError("Manifest must contain exactly the two ordered catalogues")
    allowed_paths = {Path("manifest.json")}
    counts = {}
    for kind, slug, _ in CATALOGUES:
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
        if len(collected) < minimum_items:
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
        counts[kind] = len(collected)
    actual_paths = {p.relative_to(output) for p in output.rglob("*") if p.is_file()}
    if actual_paths != allowed_paths or any(p.is_symlink() for p in output.rglob("*")):
        raise RefreshError("Unexpected file or link in public output")
    return counts


def promote_output(stage: Path, output: Path) -> None:
    """Stage a complete validated directory, with rollback if promotion fails.

    Git publication is one commit after both rows validate. The local directory
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
    if result.get("catalogue_order") != ["series", "movie"] or set(result.get("catalogues", {})) != {"series", "movie"}:
        raise RefreshError("Generator did not return exactly two ordered catalogues")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + output.name + ".stage.", dir=output.parent) as temporary:
        stage = Path(temporary) / "output"
        stage.mkdir()
        atomic_json(stage / "manifest.json", manifest(addon_id))
        for kind, slug, title in CATALOGUES:
            catalogue = result["catalogues"][kind]
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
    private_diagnostics: Path | None = None,
) -> dict[str, int]:
    if generator is None:
        from trakt_suggested_engine import generate_catalogues
        generator = generate_catalogues
    failures.require_success()
    for name in INPUT_FILES:
        if name not in histories or not isinstance(histories[name], list):
            raise SourceError("A required history list is missing")
    result = generator(
        histories["watched_movies"], histories["watched_shows"],
        histories["rated_movies"], histories["rated_shows"], histories["watched_episode_history"],
        tmdb_fetch, trakt_fetch=trakt_fetch, now=now,
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
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
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
        for private_path in (args.cache_dir, args.history_dir, args.private_diagnostics):
            if private_path and (private_path.resolve() == public_root or
                                 public_root in private_path.resolve().parents):
                raise RefreshError("Private files cannot be stored in public output")
        failures = SourceFailures()
        deadline = time.monotonic() + args.timeout_minutes * 60
        trakt = TraktClient(JsonHttpClient(
            "https://api.trakt.tv", {"trakt-api-version": "2", "trakt-api-key": trakt_key},
            "trakt", failures, deadline=deadline,
        ))
        tmdb = TmdbClient(JsonHttpClient(
            "https://api.themoviedb.org/3", {"Authorization": "Bearer " + tmdb_token} if tmdb_token else {},
            "tmdb", failures, extra_query={} if tmdb_token else {"api_key": tmdb_key}, deadline=deadline,
        ), args.cache_dir, args.cache_ttl_hours)
        histories = load_history_snapshots(args.history_dir) if args.history_dir else trakt.histories(username)
        counts = generate_and_publish(
            histories, tmdb, trakt.seasons, args.output, failures=failures,
            now=datetime.now(timezone.utc), addon_id=args.addon_id, private_diagnostics=args.private_diagnostics,
        )
        print(json.dumps({"published": True, "counts": counts}))
        return 0
    except Exception as error:
        # Errors can carry credential-bearing request URLs or private titles.
        # Print only the exception class; do not dump engine diagnostics or traces.
        print("Refresh aborted (" + type(error).__name__ + "); no new catalogue generation was published.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
