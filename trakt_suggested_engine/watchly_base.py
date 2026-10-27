# Watchly-derived logic: Copyright (c) 2025 Bimal Timilsina (MIT).
# Full upstream notice: LICENSE-Watchly. Python replay is a local adaptation.
"""Local, standard-library Watchly preview; no network, accounts or files opened.

Pinned algorithm: TimilsinaBimal/Watchly
74f7201232c4437976c647a6f6c268285823c328 (read 2026-10-18).

The caller supplies TMDB callbacks, ordinarily backed by its own existing API
access/cache. No history is submitted to Watchly, Simkl, an LLM, or another
recommendation service. Callbacks receive individual media IDs/discovery queries.

API
---
    history = history_from_trakt(watched_movies, watched_shows,
                                 rated_movies, rated_shows)
    api = TMDBCallbacks(resolve, details, recommendations, discover, similar)
    result = preview(history, api, "movie", policy=ste_policy("movie"),
                     now="2026-10-18T04:30:00Z", max_workers=8, limit=20)

Callback signatures (synchronous; 'media_type' is 'movie' or 'tv'):
    resolve(imdb_id, media_type) -> integer TMDB ID or None
    details(tmdb_id, media_type) -> TMDB details mapping, with credits,
        keywords, external_ids and created_by where applicable
    recommendations(tmdb_id, media_type, page) -> {'results': [...]} or list
    discover(media_type, params_mapping) -> {'results': [...]} or list
    similar(tmdb_id, media_type, page) -> {'results': [...]} or list
All must be safe for concurrent calls if max_workers > 1. No credentials should
be embedded in exception strings. Errors are recorded as exception TYPE only.

Fidelity and intentional differences
-----------------------------------
* Replays fresh external-history profile, 15-item seed sampler, TMDB candidate
  acquisition, source scorer and genre diversity stage. Simkl/LLM off.
* Exact source bucketing is preserved: >=9 loved; 7-8 liked; an unrated item
  with plays>=2 becomes loved. Other ratings lose their numeric distinction in
  the source converter. We do not invent missing ratings or watch dates.
* Ranks RAW candidate data before detailed enrichment, as the pinned source
  does. Do not silently enrich candidates first: it changes their scores.
* Runtime/episode extraction from Cinemeta is omitted: those fields do not
  affect the pinned Top Picks candidate queries or final scorer.
* This is a fresh build, without Watchly's historical cache/decay state,
  optional providers, translation/poster enrichment or failure fallback.
* Source Top Picks requests 20 by default but selects up to 60 before final
  filtering. Here 'limit' controls that same stage and 'display_limit' controls
  how many final titles the returned preview shows (default=limit).
* Because-watched uses a reproducible random seed (default 0), or an explicitly
  chosen eligible title; the hosted service randomly reselects on rebuild.
* FilterPolicy supports EXTRA constraints absent from ordinary Watchly setup:
  positive genre whitelist, original-language restriction, exact release-day
  exclusion, custom quality thresholds and newest-first display. ste_policy()
  intentionally enables these to match Ste's current discovery requirements.
  The result labels them explicitly; do not call that an unmodified hosted row.
* No promise that the hosted service uses this commit or will show this order.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import math
import random
import threading
from typing import Any, Callable, Iterable, Mapping, Optional


COMMIT = "74f7201232c4437976c647a6f6c268285823c328"
SOURCE_ROOT = "https://github.com/TimilsinaBimal/Watchly/blob/" + COMMIT + "/"
SOURCE_FILES = [
    "app/services/trakt.py", "app/services/stremio/library.py",
    "app/services/profile/service.py", "app/services/profile/sampling.py",
    "app/services/profile/scoring.py", "app/services/profile/evidence.py",
    "app/services/profile/builder.py", "app/services/profile/vectorizer.py",
    "app/services/profile/scorer.py", "app/models/profile.py",
    "app/services/recommendation/candidate_sources.py",
    "app/services/recommendation/scoring.py",
    "app/services/recommendation/diversity.py",
    "app/services/recommendation/top_picks.py",
    "app/services/recommendation/item_based.py",
    "app/services/catalog_definitions.py", "app/core/constants.py",
]
DISCOVERY = {
    "mainstream": {"vote_average.gte": 6.2, "vote_count.gte": 1500},
    "balanced": {"vote_average.gte": 6.7, "vote_count.gte": 250},
    "gems": {"vote_average.gte": 7.2, "vote_count.gte": 150, "vote_count.lte": 4000},
    "all": {"vote_average.gte": 5.0, "vote_count.gte": 50},
}
CAPS = {"genres": 50., "keywords": 40., "directors": 30., "cast": 30.,
        "eras": 25., "countries": 20.}
ERA_STARTS = {"pre-1970s": 1950, "1970s": 1970, "1980s": 1980,
              "1990s": 1990, "2000s": 2000, "2010s": 2010, "2020s": 2020}


@dataclass(frozen=True)
class TMDBCallbacks:
    resolve: Callable[[str, str], Optional[int]]
    details: Callable[[int, str], Mapping[str, Any]]
    recommendations: Callable[[int, str, int], Any]
    discover: Callable[[str, Mapping[str, Any]], Any]
    similar: Optional[Callable[[int, str, int], Any]] = None


def callbacks_from_fetch(fetch: Callable[[str, Optional[Mapping[str, Any]]], Any],
                         language: str = "en-US") -> TMDBCallbacks:
    """Adapt fetch(path, params=None) to this module's interface.

    Paths are relative to https://api.themoviedb.org/3, with a leading slash.
    The caller injects its existing API key; this module never receives it.
    No original-language restriction is implied by metadata language.
    """
    def resolve(imdb: str, media: str) -> Optional[int]:
        result = fetch("/find/" + imdb, {"external_source": "imdb_id", "language": language})
        choices = result.get("movie_results" if media == "movie" else "tv_results", []) if isinstance(result, Mapping) else []
        return _int(choices[0].get("id")) if choices else None

    def details(tid: int, media: str) -> Any:
        return fetch("/" + media + "/" + str(tid),
                     {"append_to_response": "credits,keywords,external_ids", "language": language})

    def related(kind: str, tid: int, media: str, page: int) -> Any:
        return fetch("/" + media + "/" + str(tid) + "/" + kind,
                     {"page": page, "language": language})

    def discover(media: str, params: Mapping) -> Any:
        return fetch("/discover/" + media, dict(params, language=language))

    return TMDBCallbacks(resolve, details,
                         lambda tid, media, page: related("recommendations", tid, media, page),
                         discover,
                         lambda tid, media, page: related("similar", tid, media, page))


@dataclass(frozen=True)
class FilterPolicy:
    year_min: Optional[int] = 1970
    year_max: Optional[int] = None
    discovery_style: Optional[str] = "balanced"
    required_any_genres: tuple[int, ...] = ()
    excluded_genres: tuple[int, ...] = ()
    original_language: Optional[str] = None
    min_rating: Optional[float] = None
    min_votes: Optional[int] = None
    max_votes: Optional[int] = None
    released_only: bool = False
    newest_first: bool = False

    def quality(self) -> dict[str, float]:
        band = dict(DISCOVERY.get(self.discovery_style or "", {}))
        for key, value in (("vote_average.gte", self.min_rating),
                           ("vote_count.gte", self.min_votes),
                           ("vote_count.lte", self.max_votes)):
            if value is not None:
                band[key] = value
        return band

    def differences(self) -> list[str]:
        differences = []
        if self.required_any_genres:
            differences.append("Extra positive genre whitelist from Ste's current catalogue")
        if self.original_language:
            differences.append("Extra original-language gate; Watchly language normally translates metadata")
        if self.released_only:
            differences.append("Extra exact release-date gate, including exclusion of unknown release dates")
        if any(x is not None for x in (self.min_rating, self.min_votes, self.max_votes)):
            differences.append("Custom quality thresholds from Ste's current catalogue")
        if self.newest_first:
            differences.append("Newest-first display after Watchly selects candidates; source rank retained separately")
        if self.year_min is None:
            differences.append("No lower release-year limit; Watchly default starts at 1970")
        return differences


def ste_policy(media_type: str, *, original_language: Optional[str] = "en",
               newest_first: bool = True) -> FilterPolicy:
    """Exact recovered personal-discovery gates; UK police factual stays separate.

    This adapter's optional 'en' gate means ORIGINAL language, not translations.
    Pass original_language=None to compare without that extra condition.
    """
    movie = media_type == "movie"
    if media_type not in ("movie", "series", "tv", "show"):
        raise ValueError("media_type must be movie or series")
    return FilterPolicy(
        year_min=None, discovery_style=None,
        required_any_genres=(80, 53, 9648) if movie else (80, 9648),
        excluded_genres=(16, 27, 14, 878, 10751) if movie else
                        (16, 99, 10762, 10763, 10764, 10765, 10766, 10767),
        original_language=original_language, min_rating=7.0,
        min_votes=100 if movie else 30, released_only=True,
        newest_first=newest_first,
    )


def _dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _num(value: Any, default: float = 0.) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _rows(response: Any) -> list[dict[str, Any]]:
    rows = response.get("results", []) if isinstance(response, Mapping) else response
    return [dict(r) for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def history_from_trakt(watched_movies: Iterable[Mapping] = (),
                       watched_shows: Iterable[Mapping] = (),
                       rated_movies: Iterable[Mapping] = (),
                       rated_shows: Iterable[Mapping] = ()) -> list[dict[str, Any]]:
    """Mirror Trakt watched+ratings merge, retaining known TMDB IDs as an optimisation."""
    watched_movies, watched_shows = list(watched_movies), list(watched_shows)
    rated = list(rated_movies) + list(rated_shows)
    rating_map, rated_dates = {}, {}
    for entry in rated:
        media = entry.get("movie") or entry.get("show") or {}
        imdb = (media.get("ids") or {}).get("imdb")
        if imdb and entry.get("rating"):
            rating_map[imdb] = _num(entry["rating"])
            rated_dates[imdb] = entry.get("rated_at")
    result, seen = [], set()
    for content, entries in (("movie", watched_movies), ("series", watched_shows)):
        for entry in entries:
            media = entry.get("movie") or entry.get("show") or {}
            ids = media.get("ids") or {}
            imdb = ids.get("imdb")
            if not imdb or imdb in seen:
                continue
            seen.add(imdb)
            result.append(dict(imdb_id=imdb, tmdb_id=_int(ids.get("tmdb")),
                               type=content, name=media.get("title", ""),
                               rating=rating_map.get(imdb),
                               last_rated=rated_dates.get(imdb),
                               watch_count=entry.get("plays", 1), completion=1.0,
                               last_watched=entry.get("last_watched_at"), source="trakt"))
    for entry in rated:
        media = entry.get("movie") or entry.get("show") or {}
        ids = media.get("ids") or {}
        imdb = ids.get("imdb")
        if not imdb or imdb in seen:
            continue
        seen.add(imdb)
        result.append(dict(imdb_id=imdb, tmdb_id=_int(ids.get("tmdb")),
                           type="movie" if "movie" in entry else "series",
                           name=media.get("title", ""), rating=_num(entry.get("rating")),
                           watch_count=0, completion=0., last_watched=entry.get("rated_at"),
                           last_rated=entry.get("rated_at"),
                           source="trakt", rated_without_watched=True))
    return result


def _bucket(item: Mapping) -> str:
    rating = item.get("rating")
    if rating is not None and _num(rating) >= 9:
        return "loved"
    if rating is not None and _num(rating) >= 7:
        return "liked"
    if rating is None and _num(item.get("watch_count"), 1) >= 2:
        return "loved"
    return "watched"


def _completion(item: Mapping) -> float:
    comp = int(6000 * min(1., max(0., _num(item.get("completion"), 1.)))) / 6000.
    if _num(item.get("watch_count"), 1) > 0 and comp < .5:
        comp = .5
    return comp


def interest_score(item: Mapping, now: datetime) -> float:
    """Pinned source's seed-sampling score after external history conversion."""
    completion = _completion(item)
    plays = max(0., _num(item.get("watch_count"), 1))
    rewatch = min(100., max(0., (plays - 1) * 50.))
    last = _dt(item.get("last_watched"))
    recent = 100 * math.exp(-max(0, (now - last).days) / 60.) if last else 0.
    explicit = {"loved": 100., "liked": 70.}.get(_bucket(item), 0.)
    return min(100., max(0., completion * 10 + rewatch * .17 + recent * .30 + explicit * .35 + 8))


def evidence_weight(item: Mapping, now: datetime) -> float:
    bucket = _bucket(item)
    comp = _completion(item)
    if bucket == "loved":
        base = 2.5
    elif bucket == "liked":
        base = 1.5
    else:
        raw_comp = int(6000 * min(1., max(0., _num(item.get("completion"), 1.)))) / 6000.
        minutes = raw_comp * 100 if raw_comp > 0 else None
        if minutes is not None and minutes < 15:
            return 0.
        if comp >= .7:
            base = 1.
        elif comp < .3 and minutes is not None and minutes >= 15:
            base = -.2
        else:
            base = 1. if comp >= .8 else .5 if comp >= .4 else .3
    last = _dt(item.get("last_watched"))
    decay = max(.1, math.exp(-max(0, (now - last).days) / 30.)) if last else .5
    return base * decay


def sample_seeds(history: Iterable[Mapping], now: datetime, cap: int = 15) -> list[dict]:
    pools = {name: [] for name in ("loved", "liked", "watched")}
    seen = set()
    for raw in history:
        item = dict(raw)
        imdb = item.get("imdb_id")
        if not imdb or imdb in seen:
            continue
        seen.add(imdb)
        pools[_bucket(item)].append(item)
    all_items = pools["loved"] + pools["liked"] + pools["watched"]
    if len(all_items) <= cap:
        return all_items
    rated = sorted(pools["loved"] + pools["liked"], key=lambda i: interest_score(i, now), reverse=True)
    watched = sorted(pools["watched"], key=lambda i: interest_score(i, now), reverse=True)
    rated_quota, added_quota = int(cap * .55), int(cap * .10)
    selected = rated[:rated_quota] + watched[:cap - rated_quota - added_quota]
    used = {i["imdb_id"] for i in selected}
    for item in rated + watched:
        if len(selected) >= cap:
            break
        if item["imdb_id"] not in used:
            selected.append(item)
            used.add(item["imdb_id"])
    return selected


def _era(year: int) -> str:
    return "pre-1970s" if year < 1970 else str(min(2020, year // 10 * 10)) + "s"


def _release(item: Mapping) -> str:
    return str(item.get("release_date") or item.get("first_air_date") or item.get("released") or "")


def _genres(item: Mapping) -> list[int]:
    return [int(x) for x in item.get("genre_ids", [])] if "genre_ids" in item else [
        int(g["id"]) for g in item.get("genres", []) if isinstance(g, Mapping) and g.get("id")]


def _keywords(item: Mapping, *, profile: bool = False) -> list[int]:
    direct = item.get("keyword_ids", [])
    if direct:
        return direct
    keywords = item.get("keywords") or {}
    if isinstance(keywords, Mapping):
        keywords = (keywords.get("results") or keywords.get("keywords") or []) if profile else keywords.get("keywords", [])
    return [k["id"] for k in keywords if isinstance(k, Mapping) and k.get("id")] if isinstance(keywords, list) else []


def _countries(item: Mapping) -> list[str]:
    return [c.get("iso_3166_1") if isinstance(c, Mapping) else c
            for c in item.get("production_countries", []) if c]


def passes_filters(item: Mapping, policy: FilterPolicy, now: datetime) -> bool:
    release = _release(item)
    year = _int(release[:4])
    if year is not None and ((policy.year_min is not None and year < policy.year_min)
                             or year > (policy.year_max or now.year)):
        return False
    if policy.released_only:
        try:
            if date.fromisoformat(release[:10]) > now.date():
                return False
        except ValueError:
            return False
    genres = set(_genres(item))
    if policy.required_any_genres and not genres.intersection(policy.required_any_genres):
        return False
    if genres.intersection(policy.excluded_genres):
        return False
    if policy.original_language and item.get("original_language") != policy.original_language:
        return False
    for key, threshold in policy.quality().items():
        field, operator = key.split(".")
        if item.get(field) is None:
            return False
        value = _num(item[field])
        if (operator == "gte" and value < threshold) or (operator == "lte" and value > threshold):
            return False
    return True


def weighted_rating(average: Any, votes: Any, prior: float = 6.8) -> float:
    n = max(0., _num(votes))
    return (n * _num(average) + 150 * prior) / (n + 150)


def score_candidate(item: Mapping, normalised_profile: Mapping, media_type: str) -> tuple[float, dict]:
    """Source scorer. Candidate is raw TMDB row, deliberately not details-enriched."""
    def mean_match(category: str, ids: Iterable) -> float:
        ids = list(ids)
        return sum(normalised_profile.get(category, {}).get(i, 0.) for i in ids) / len(ids) if ids else 0.
    credits = item.get("credits") or {}
    cast = [c.get("id") for c in credits.get("cast", [])[:5] if isinstance(c, Mapping) and c.get("id")]
    directors = [c.get("id") for c in credits.get("crew", []) if isinstance(c, Mapping) and c.get("id")
                 and str(c.get("job", "")).lower() in ("director", "creator", "producer")]
    year = _int(_release(item)[:4])
    parts = {
        "genre_match": .9 * mean_match("genres", item.get("genre_ids", [])),
        "keyword_match": .7 * mean_match("keywords", _keywords(item)),
        "cast_match": .9 * mean_match("cast", cast),
        "director_match": .9 * mean_match("directors", directors),
        "era_match": .6 * mean_match("eras", [_era(year)] if year else []),
        "country_match": .3 * mean_match("countries", _countries(item)),
    }
    profile_score = sum(parts.values())
    wr = weighted_rating(item.get("vote_average"), item.get("vote_count"), 7.2 if media_type == "movie" else 6.8)
    score = .70 * profile_score + .30 * min(1., max(0., wr / 10.))
    popularity, votes = _num(item.get("popularity")), _num(item.get("vote_count"))
    boost = 1.05 if popularity > 500 and wr > 7.5 else 1.10 if votes >= 100 and wr >= 7 and popularity <= 100 else 1.
    return score * boost, dict(parts, profile_score=profile_score, weighted_rating=wr, boost=boost)


class WatchlyPreview:
    # Profile evidence per history item; a taste profile may replace it.
    evidence = staticmethod(evidence_weight)

    def __init__(self, api: TMDBCallbacks, policy: FilterPolicy, now: Any = None, max_workers: int = 1):
        self.api, self.policy = api, policy
        self.now = _dt(now) or datetime.now(timezone.utc)
        self.max_workers = max(1, min(30, int(max_workers)))
        self.warnings: list[dict] = []
        self.calls: Counter = Counter()
        self._lock = threading.Lock()
        self._detail_cache: dict[tuple, dict] = {}

    def _call(self, name: str, *args: Any) -> Any:
        function = getattr(self.api, name)
        if function is None:
            return None
        with self._lock:
            self.calls[name] += 1
        try:
            return function(*args)
        except Exception as error:
            with self._lock:
                self.warnings.append({"stage": name, "exception_type": type(error).__name__})
            return None

    def _map(self, function: Callable, items: Iterable) -> list:
        items = list(items)
        if self.max_workers == 1:
            return [function(item) for item in items]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(function, items))

    def _details(self, tmdb_id: int, mtype: str) -> dict:
        key = (mtype, tmdb_id)
        with self._lock:
            cached = self._detail_cache.get(key)
        if cached is not None:
            return cached
        value = self._call("details", tmdb_id, mtype)
        result = dict(value) if isinstance(value, Mapping) else {}
        with self._lock:
            self._detail_cache[key] = result
        return result

    def _resolve(self, item: Mapping, mtype: str) -> Optional[int]:
        return _int(item.get("tmdb_id")) or _int(self._call("resolve", item["imdb_id"], mtype))

    def _profile(self, history: list[dict], mtype: str) -> tuple[dict, dict, dict]:
        scores = {k: {} for k in CAPS}
        counts = {k: Counter() for k in CAPS}
        resolved, processed = {}, []
        def metadata(item):
            tid = self._resolve(item, mtype)
            return item, tid, self._details(tid, mtype) if tid else {}
        for item, tid, detail in self._map(metadata, history):
            if tid:
                resolved[item["imdb_id"]] = tid
            if not detail:
                self.warnings.append({"stage": "profile", "imdb_id": item["imdb_id"], "reason": "Missing TMDB details"})
                continue
            processed.append(item["imdb_id"])
            evidence = self.evidence(item, self.now)
            def add(category, key, factor):
                if key:
                    scores[category][key] = scores[category].get(key, 0.) + evidence * factor
                    counts[category][key] += 1
            for position, genre in enumerate(_genres(detail)[:3]):
                add("genres", genre, .9 * (1., .8, .5)[position])
            for keyword in _keywords(detail, profile=True):
                add("keywords", keyword, .7)
            for country in _countries(detail):
                add("countries", country, .3)
            year = _int(_release(detail)[:4])
            if year:
                add("eras", _era(year), .6)
            credits = detail.get("credits") or {}
            for position, actor in enumerate(credits.get("cast", [])[:3]):
                if isinstance(actor, Mapping):
                    add("cast", actor.get("id"), .9 * .7 ** position)
            crew = [dict(c, job="Creator") for c in detail.get("created_by", []) if isinstance(c, Mapping)] + list(credits.get("crew", []))
            for person in crew:
                if isinstance(person, Mapping) and str(person.get("job", "")).lower() in ("director", "creator"):
                    add("directors", person.get("id"), .9)
        for category, values in scores.items():
            for key, value in values.items():
                frequency = counts[category][key]
                if category in ("genres", "keywords", "directors", "cast") and frequency > 1:
                    value *= 1 + .1 * math.log(frequency)
                values[key] = max(-CAPS[category], min(CAPS[category], value))
        normalized = {}
        for category, values in scores.items():
            maximum = max(values.values(), default=0.)
            normalized[category] = {k: v / maximum if maximum > 0 else v for k, v in values.items()}
        return scores, normalized, {"resolved": resolved, "processed": processed}

    def _discover_queries(self, profile: Mapping, mtype: str) -> list[dict]:
        def top(category, n):
            return [key for key, _ in sorted(profile[category].items(), key=lambda p: p[1], reverse=True)[:n]]
        raw = []
        for category, count, field, pages in (("genres", 5, "with_genres", (1,)),
                ("keywords", 5, "with_keywords", (1, 2)), ("directors", 3, "with_crew", (1,)),
                ("cast", 5, "with_cast", (1,)), ("countries", 5, "with_origin_country", (1,))):
            keys = top(category, count)
            if keys:
                raw.extend({field: "|".join(map(str, keys)), "page": page} for page in pages)
        # Preserve source ordering: countries follows the era query.
        country_query = raw.pop() if raw and "with_origin_country" in raw[-1] else None
        eras = top("eras", 2)
        if eras:
            year = ERA_STARTS.get(eras[0])
            if year:
                prefix = "primary_release_date" if mtype == "movie" else "first_air_date"
                raw.append({prefix + ".gte": str(year) + "-01-01",
                            prefix + ".lte": self.now.date().isoformat() if year + 9 > self.now.year else str(year + 9) + "-12-31", "page": 1})
        if country_query:
            raw.append(country_query)
        base = dict(self.policy.quality())
        base["sort_by"] = "vote_average.desc" if self.policy.discovery_style == "gems" else "popularity.desc"
        for prefix in ("primary_release_date", "first_air_date"):
            if self.policy.year_min is not None:
                base[prefix + ".gte"] = str(self.policy.year_min) + "-01-01"
            base[prefix + ".lte"] = self.now.date().isoformat() if (self.policy.year_max or self.now.year) >= self.now.year else str(self.policy.year_max) + "-12-31"
        if self.policy.excluded_genres:
            base["without_genres"] = "|".join(map(str, self.policy.excluded_genres))
        if self.policy.original_language:
            base["with_original_language"] = self.policy.original_language
        # Additional positive genres are gated after fetch. Overriding the profile
        # genre query with them would change what the source algorithm fetches.
        return [dict(base, **query) for query in raw]

    def _unseen(self, raw: Mapping, detail: Mapping, watched_imdb: set, watched_tmdb: set) -> bool:
        tid = _int(raw.get("id")) or _int(detail.get("id"))
        imdb = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
        return tid not in watched_tmdb and (not imdb or imdb not in watched_imdb)

    def _card(self, raw: Mapping, detail: Mapping) -> dict:
        tid = _int(raw.get("id")) or _int(detail.get("id"))
        imdb = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
        poster = detail.get("poster_path") or raw.get("poster_path")
        return {"id": imdb or "tmdb:" + str(tid), "tmdb_id": tid, "imdb_id": imdb,
                "title": detail.get("title") or detail.get("name") or raw.get("title") or raw.get("name"),
                "release_date": _release(detail) or _release(raw),
                "year": _int((_release(detail) or _release(raw))[:4]),
                "genre_ids": _genres(detail) or _genres(raw),
                "genres": [g.get("name") for g in detail.get("genres", []) if isinstance(g, Mapping)],
                "original_language": detail.get("original_language") or raw.get("original_language"),
                "vote_average": raw.get("vote_average", detail.get("vote_average")),
                "vote_count": raw.get("vote_count", detail.get("vote_count")),
                "poster": "https://image.tmdb.org/t/p/w500" + poster if poster else None,
                "overview": detail.get("overview") or raw.get("overview") or ""}

    def top_picks(self, history: list[dict], profile: Mapping, normalized: Mapping,
                  resolved: Mapping, mtype: str, limit: int, display_limit: int) -> dict:
        seeds = sample_seeds(history, self.now, 15)
        candidates, origins, encountered = {}, {}, {}
        def merge(items, origin):
            for item in items:
                tid = _int(item.get("id"))
                if tid:
                    encountered[tid] = item
                    origins.setdefault(tid, []).append(origin)
                    if passes_filters(item, self.policy, self.now):
                        candidates[tid] = item
        def fetch_seed(item):
            tid = resolved.get(item["imdb_id"]) or self._resolve(item, mtype)
            return item, _rows(self._call("recommendations", tid, mtype, 1)) if tid else []
        for item, rows in self._map(fetch_seed, seeds):
            merge(rows, "Seed: " + str(item.get("name") or item["imdb_id"]))
        queries = self._discover_queries(profile, mtype)
        for query, rows in self._map(lambda q: (q, _rows(self._call("discover", mtype, q))), queries):
            merge(rows, "Discover: " + ", ".join(k for k in query if k.startswith("with_") or k.startswith("primary_release_date.gte") or k.startswith("first_air_date.gte")))
        watched_imdb = {i["imdb_id"] for i in history}
        watched_tmdb = set(resolved.values())
        ranked = []
        for tid, item in candidates.items():
            if tid in watched_tmdb:
                continue
            score, parts = score_candidate(item, normalized, mtype)
            ranked.append((score, item, parts))
        ranked.sort(key=lambda r: r[0], reverse=True)
        target = limit * 3
        genre_cap, genre_counts, selected = int(target * .50), Counter(), []
        quality = self.policy.quality()
        minimum_rating, minimum_votes = quality.get("vote_average.gte", 6.7), quality.get("vote_count.gte", 250)
        for score, item, parts in ranked:
            if len(selected) >= target:
                break
            if _num(item.get("vote_count")) < minimum_votes:
                continue
            gate_rating = weighted_rating(item.get("vote_average"), item.get("vote_count"), 6.8 if mtype == "movie" else 7.2)
            if gate_rating < minimum_rating:
                continue
            genres = item.get("genre_ids", [])
            primary_genre = genres[0] if genres else None
            if primary_genre and genre_counts[primary_genre] >= genre_cap:
                continue
            selected.append((score, item, parts))
            if primary_genre:
                genre_counts[primary_genre] += 1
        cards = []
        def enrich(row):
            return row, self._details(int(row[1]["id"]), mtype)
        for (score, item, parts), detail in self._map(enrich, selected):
            if not detail:
                continue
            # Keep every additional user gate true on final metadata as well.
            combined = dict(item, **detail)
            combined["genre_ids"] = _genres(detail) or _genres(item)
            if not self._unseen(item, detail, watched_imdb, watched_tmdb) or not passes_filters(combined, self.policy, self.now):
                continue
            card = self._card(item, detail)
            card.update(score=score, score_parts=parts, candidate_sources=origins.get(int(item["id"]), []), source_rank=len(cards) + 1)
            cards.append(card)
        source_cards = cards[:100]
        display = sorted(source_cards, key=lambda c: (c["release_date"], c["score"]), reverse=True) if self.policy.newest_first else source_cards
        scored_map = {int(item["id"]): (score, parts) for score, item, parts in ranked}
        selected_ids = {int(item["id"]) for _, item, _ in selected}
        eligible_ids = {card["tmdb_id"] for card in source_cards}
        diagnostics = []
        for tid, item in encountered.items():
            scored = scored_map.get(tid)
            diagnostics.append({"tmdb_id": tid,
                "title": item.get("title") or item.get("name"),
                "release_date": _release(item), "genre_ids": _genres(item),
                "original_language": item.get("original_language"),
                "vote_average": item.get("vote_average"), "vote_count": item.get("vote_count"),
                "passed_policy": tid in candidates, "known_watched": tid in watched_tmdb,
                "selected_before_enrichment": tid in selected_ids, "eligible_final": tid in eligible_ids,
                "score": scored[0] if scored else None, "score_parts": scored[1] if scored else None,
                "candidate_sources": origins.get(tid, [])})
        return {"seeds": [dict(i, bucket=_bucket(i), interest_score=interest_score(i, self.now)) for i in seeds],
                "discover_queries": queries, "candidate_count_raw_unique": len(encountered),
                "candidate_count_after_policy": len(candidates),
                "unwatched_scored_count": len(ranked), "selected_before_enrichment": len(selected),
                "eligible_count": len(source_cards), "items": display[:display_limit],
                "source_rank_items": source_cards[:display_limit],
                "all_candidate_diagnostics": diagnostics}

    def because_watched(self, history: list[dict], resolved: Mapping, mtype: str,
                        display_limit: int, seed_imdb: Optional[str] = None,
                        random_seed: int = 0) -> dict:
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        recent = lambda item: _dt(item.get("last_watched")) or minimum
        pool = sorted([i for i in history if _bucket(i) == "loved"], key=recent, reverse=True)[:3]
        pool += sorted([i for i in history if _bucket(i) == "watched"], key=recent, reverse=True)[:3]
        if not pool:
            return {"items": [], "reason": "No eligible loved/watched seed", "eligible_seeds": []}
        if seed_imdb:
            matches = [i for i in pool if i["imdb_id"] == seed_imdb]
            if not matches:
                raise ValueError("The chosen Because-watched seed is not in the source's eligible recent seed pool")
            seed = matches[0]
        else:
            seed = random.Random(random_seed).sample(pool, 1)[0]
        tid = resolved.get(seed["imdb_id"]) or self._resolve(seed, mtype)
        merged = {}
        def fetch_pages(name, pages):
            for rows in self._map(lambda page: _rows(self._call(name, tid, mtype, page)), pages):
                for item in rows:
                    if item.get("id"):
                        merged[int(item["id"])] = item
        if tid:
            fetch_pages("recommendations", (1, 2, 3))
            if len(merged) < 30 and self.api.similar:
                fetch_pages("similar", (1, 2, 3))
            if sum(passes_filters(i, self.policy, self.now) for i in merged.values()) < 30 and self.api.similar:
                fetch_pages("similar", (4, 5, 6))
        watched_imdb, watched_tmdb = {i["imdb_id"] for i in history}, set(resolved.values()) | {tid}
        filtered = [item for key, item in merged.items() if key not in watched_tmdb and passes_filters(item, self.policy, self.now)]
        cards = []
        for item, detail in self._map(lambda row: (row, self._details(int(row["id"]), mtype)), filtered):
            combined = dict(item, **detail)
            combined["genre_ids"] = _genres(detail) or _genres(item)
            if detail and self._unseen(item, detail, watched_imdb, watched_tmdb) and passes_filters(combined, self.policy, self.now):
                card = self._card(item, detail)
                card["source_rank"] = len(cards) + 1
                cards.append(card)
        display = sorted(cards, key=lambda c: c["release_date"], reverse=True) if self.policy.newest_first else cards
        return {"seed": dict(seed, bucket=_bucket(seed)),
                "label": ("Because you loved " if _bucket(seed) == "loved" else "Because you watched ") + str(seed.get("name") or seed["imdb_id"]),
                "seed_selection": "Explicit eligible seed" if seed_imdb else "Reproducible sample with random_seed=" + str(random_seed),
                "eligible_seeds": [dict(i, bucket=_bucket(i)) for i in pool],
                "candidate_count": len(merged), "eligible_count": len(cards),
                "items": display[:display_limit], "source_rank_items": cards[:display_limit]}

    def run(self, history: Iterable[Mapping], media_type: str, *, limit: int = 20,
            display_limit: Optional[int] = None, include_because: bool = True,
            because_seed_imdb: Optional[str] = None, random_seed: int = 0) -> dict:
        mtype = "movie" if media_type == "movie" else "tv"
        content = "movie" if mtype == "movie" else "series"
        if media_type not in ("movie", "series", "show", "tv"):
            raise ValueError("media_type must be movie or series")
        if limit < 1:
            raise ValueError("limit must be positive")
        display_limit = limit if display_limit is None else max(1, int(display_limit))
        unique = {}
        for raw in history:
            item = dict(raw)
            if item.get("type") in ("tv", "show"):
                item["type"] = "series"
            if item.get("type") == content and item.get("imdb_id") and item["imdb_id"] not in unique:
                unique[item["imdb_id"]] = item
        # External LibraryCollection order is loved, liked, watched.
        items = [i for b in ("loved", "liked", "watched") for i in unique.values() if _bucket(i) == b]
        profile, normalized, audit = self._profile(items, mtype)
        result = {
            "algorithm": "Local Watchly fresh-build TMDB-only replay",
            "commit": COMMIT, "media_type": content,
            "generated_at": self.now.isoformat(), "policy": asdict(self.policy),
            "extra_constraints": self.policy.differences(),
            "history_count": len(items), "rated_count": sum(i.get("rating") is not None for i in items),
            "unknown_watch_date_count": sum(_dt(i.get("last_watched")) is None for i in items),
            "profile_item_count": len(audit["processed"]),
            "profile": profile, "normalised_profile": normalized,
            "source_urls": [SOURCE_ROOT + p for p in SOURCE_FILES],
        }
        if items and audit["processed"]:
            result["top_picks"] = self.top_picks(items, profile, normalized, audit["resolved"], mtype, limit, display_limit)
            if include_because:
                result["because_watched"] = self.because_watched(items, audit["resolved"], mtype, display_limit, because_seed_imdb, random_seed)
        else:
            result["top_picks"] = {"items": [], "reason": "No usable history profile; no generic fallback inserted"}
        result["warnings"], result["callback_counts"] = self.warnings, dict(self.calls)
        result["limitations"] = [
            "Preview reflects only the input history and current callback data; hosted lists were not queried.",
            "Fresh profile without Watchly cache history; Simkl, LLM, runtime, translation and poster services omitted.",
            "Source's coarse rating/rewatch bucketing is preserved, including its handling of Trakt show plays.",
            "Any custom constraints above adapt Watchly's output; they are not all native Watchly options.",
            "Callback failures reduce the candidate/profile data and are reported; no fabricated results or placeholder titles.",
        ]
        return result


def preview(history: Iterable[Mapping], api: TMDBCallbacks, media_type: str,
            *, policy: Optional[FilterPolicy] = None, now: Any = None,
            max_workers: int = 1, **options: Any) -> dict:
    """Convenient single-run entry point; output is JSON serialisable."""
    return WatchlyPreview(api, policy or FilterPolicy(), now, max_workers).run(history, media_type, **options)
