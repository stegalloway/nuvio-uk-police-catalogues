# Watchly-derived logic: Copyright (c) 2025 Bimal Timilsina (MIT).
# Full upstream notice: LICENSE-Watchly. Local adaptations described in README.md.
"""Two precomputed recommendation catalogues; all I/O is injected by the caller.

This is a Watchly-based custom generator, not an authenticated Watchly client.
The upstream algorithm provides history weighting, profile construction,
candidate discovery and scoring. This module adds Ste's existing catalogue
gates, metadata-based organised-crime priority, a varied series mix, actual
watched-only exclusion and episode-aware reintroduction of later seasons. It
also reproduces the TMDB Discover queries behind the "For Ste" (For You) rows
as one more candidate source, ranked by the same weighting plus a small,
title-blind release-recency term.

The viewer-specific parts (gates, For You queries, theme weighting and series
mix) form a taste profile chosen by EngineConfig.profile. "ste" is the
default and reproduces the behaviour above exactly. "jenny" uses her own For
You queries, broader gates and a boost for true crime, murder mystery and
horror in place of the organised-crime priority.

No title or title ID is pinned, blocked or treated as a special case. No account
data, keys, files or network are read or written by this module. The caller owns
retrieval, caching, atomic publication and retention of the last good result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import math
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import watchly_base as w
from .returning_base import SeasonPremiere, WatchState, route_title


CATALOGUE_IDS = {"series": "trakt-suggested-series", "movie": "trakt-suggested-movies",
                 "upcoming": "trakt-upcoming-episodes"}
CATALOGUE_NAMES = {"series": "Trakt Suggested Series", "movie": "Trakt Suggested Movies",
                   "upcoming": "New & Upcoming Episodes"}
CATALOGUE_TYPES = {"series": "series", "movie": "movie", "upcoming": "series"}
FOR_YOU_SOURCE = "for_you_discover"


@dataclass(frozen=True)
class EngineConfig:
    catalogue_limit: int = 60
    candidate_enrichment_limit: int = 120
    max_workers: int = 4
    minimum_items: int = 5
    minimum_profile_coverage: float = 0.6
    max_returning_items: int = 6
    top_seed_count: int = 15
    thematic_seed_count: int = 4
    metadata_language: str = "en-GB"
    # Two 20-title TMDB pages: the depth of the For You rows as shown and measured.
    for_you_pages: int = 2
    # Added to every card's score, source-blind: weight * 0.5 ** (age / half-life).
    # A same-day release gains 0.20, two months 0.10, six months 0.025.
    freshness_weight: float = 0.20
    freshness_half_life_days: float = 60.0
    # Upcoming Episodes row: next episodes airing within this many days.
    upcoming_horizon_days: int = 90
    # A show stays on that row this many days after a new episode is out, as
    # "Out now", while the viewer was caught up before it and hasn't watched it.
    # Season premieres of watchlisted shows not started yet use the same window.
    out_now_days: int = 30
    # Taste profile (gates, For You queries, theme weighting): a PROFILES key.
    profile: str = "ste"


def for_you_queries(kind: str, now: datetime, pages: int) -> list[dict]:
    """TMDB Discover parameters behind the For You rows, one dict per page.

    Recovered from the saved TMDB Discover+ catalogues "For Ste - Crime &
    Thriller Movies" (tmdb-d4aecde5-...) and "For Ste - Crime & Mystery Series"
    (tmdb-c91b0ae8-...), translated exactly as Discover+ discover() does:
    'any' genres join with '|', releasedOnly adds digital release type 4 (films)
    or statuses 0|3|4|5 (series) and a release/first-air date on or before
    today (UTC). The metadata adapter adds language=en-GB, the Discover+
    config's defaultLanguage. No AIO or Discover+ URL is contacted.
    """
    today = now.date().isoformat()
    if kind == "movie":
        base = {"sort_by": "primary_release_date.desc", "include_adult": "false",
                "include_video": "false", "vote_count.gte": 100, "vote_average.gte": 7,
                "with_genres": "80|53|9648", "without_genres": "16|27|14|878|10751",
                "with_original_language": "en", "include_image_language": "en-GB,null",
                "with_release_type": "4", "primary_release_date.lte": today}
    elif kind == "series":
        base = {"sort_by": "first_air_date.desc", "include_adult": "false",
                "vote_count.gte": 30, "vote_average.gte": 7,
                "with_genres": "80|9648",
                "without_genres": "16|99|10762|10763|10764|10765|10766|10767",
                "with_original_language": "en", "include_image_language": "en-GB,null",
                "with_status": "0|3|4|5", "first_air_date.lte": today}
    else:
        raise ValueError("kind must be movie or series")
    return [dict(base, page=page) for page in range(1, max(0, pages) + 1)]


def jenny_for_you_queries(kind: str, now: datetime, pages: int) -> list[dict]:
    """TMDB Discover parameters behind Jenny's For You rows, one dict per query page.

    Her For You rows merge three TMDB Discover+ catalogues per type (Jenny
    Catalogue Extras): "True Crime", "Murder Mysteries" and "Horror". They are
    translated as Discover+ discover() does: genres join with '|' (its default
    'any' mode), keywords with '|', releasedOnly as for Ste's queries.
    imdbOnly needs no parameter: every card already requires an IMDb ID. TMDB
    TV has no horror genre, so that row uses keyword 315058 "horror". Ste asked
    for his language preference, so each query also keeps English originals
    (with_original_language=en, as his For You rows do); her Discover+ rows
    themselves have no language filter.
    """
    today = now.date().isoformat()
    if kind == "movie":
        base = {"sort_by": "primary_release_date.desc", "include_adult": "false",
                "include_video": "false", "with_original_language": "en",
                "include_image_language": "en-GB,null",
                "with_release_type": "4", "primary_release_date.lte": today}
        rows = ({"vote_count.gte": 30, "vote_average.gte": 6.5, "with_keywords": "33722"},
                {"vote_count.gte": 30, "vote_average.gte": 6.5, "with_genres": "9648|80",
                 "with_keywords": "207046|10714"},
                {"vote_count.gte": 50, "vote_average.gte": 6, "with_genres": "27"})
    elif kind == "series":
        base = {"sort_by": "first_air_date.desc", "include_adult": "false",
                "with_original_language": "en", "include_image_language": "en-GB,null",
                "with_status": "0|3|4|5", "first_air_date.lte": today}
        rows = ({"vote_count.gte": 20, "vote_average.gte": 7, "with_keywords": "33722"},
                {"vote_count.gte": 20, "vote_average.gte": 7, "with_genres": "9648|80",
                 "with_keywords": "207046|10714"},
                {"vote_count.gte": 20, "vote_average.gte": 6.5, "with_keywords": "315058"})
    else:
        raise ValueError("kind must be movie or series")
    return [dict(base, **row, page=page) for row in rows for page in range(1, max(0, pages) + 1)]


def freshness(release: Any, now: datetime, half_life_days: float) -> float:
    """1.0 on release day, halving every half_life_days; 0 if unknown or future."""
    try:
        released = date.fromisoformat(str(release or "")[:10])
    except ValueError:
        return 0.
    age = (now.date() - released).days
    return 0. if age < 0 or half_life_days <= 0 else .5 ** (age / half_life_days)


class GenerationError(RuntimeError):
    """A result must not replace the last successfully published catalogues."""

    def __init__(self, message: str, diagnostics: Mapping | None = None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def _aware(value: Any) -> datetime | None:
    """Only a timestamp with a real timezone can establish an episode release."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo and parsed.utcoffset() is not None else None


def _aliases(ids: Mapping | None) -> set[str]:
    ids = ids or {}
    return {str(kind) + ":" + str(value) for kind, value in ids.items()
            if kind in ("imdb", "tmdb", "trakt", "slug") and value is not None and str(value)}


def _show_key(show: Mapping) -> str | None:
    ids = show.get("ids") or {}
    for kind in ("trakt", "imdb", "tmdb", "slug"):
        if ids.get(kind) is not None:
            return kind + ":" + str(ids[kind])
    return None


def dismissed_ids(items: Iterable[Mapping], kind: str) -> tuple[set[str], set[int]]:
    """IMDb and TMDB IDs a "Not Interested" Trakt list hides for one kind.

    Movie entries hide films. Show, season and episode entries all carry the
    parent show, and each hides that whole series.
    """
    imdb, tmdb = set(), set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        ids = ((item.get("movie") if kind == "movie" else item.get("show")) or {}).get("ids") or {}
        if isinstance(ids.get("imdb"), str) and ids["imdb"]:
            imdb.add(ids["imdb"])
        if w._int(ids.get("tmdb")):
            tmdb.add(w._int(ids["tmdb"]))
    return imdb, tmdb


# A Not Interested title counts as a mild dislike, as strong as a title dropped
# early in Watchly's evidence weighting.
DISMISS_EVIDENCE = -0.2
# A watchlisted title counts as a mild like: half a completed watch. It does not
# fade, because a title stays on the watchlist only while it is still wanted.
WATCHLIST_EVIDENCE = 0.5
# A title on the "Loved" Trakt list is the strongest like, and does not fade
# either: it stays on the list only while it is still loved.
LOVED_EVIDENCE = 2.0

# Series evidence from how much of a show was watched (Ste, 27 October 2026).
# Trakt's show "plays" count every episode play, so they cannot tell a show
# finished from one dropped after two episodes.
VIEWING_EVIDENCE = {"finished": 1.5, "most": 1.0, "watching": .6, "dropped": -.2, "factual": 0., "unknown": 1.0}
REWATCH_BONUS = .5
# Documentary, reality, news and talk (TMDB TV genres, all excluded from Ste's
# Suggested Series): Police on Duty covers them, so they carry no taste.
FACTUAL_GENRES = frozenset({99, 10763, 10764, 10767})
# A repeat play of an episode counts as a rewatch only this long after the last
# counted play: sooner is a duplicate scrobble or a restart, not a rewatch.
REWATCH_GAP = timedelta(days=1)
TRAKT_RELATED_SOURCE = "trakt_related"
# Candidate sources enriched outside the history budget, so they never
# displace a history candidate from enrichment.
EXTRA_SOURCES = frozenset({FOR_YOU_SOURCE, TRAKT_RELATED_SOURCE})
# A Trakt community score counts only with at least this many votes.
TRAKT_RATING_VOTES = 100


def rt_bonus(score: Any, weight: float, floor: float) -> float:
    """weight scaled from floor (0) to 100 (all); a missing score adds nothing."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.
    return weight * min(1., max(0., (value - floor) / (100. - floor)))

# Ste's series taste from his Loved list (27 October 2026).
LOVED_SOURCE = "loved_recommendation"
EXTRA_SOURCES = EXTRA_SOURCES | {LOVED_SOURCE}
LOVED_PAGES = (1, 2)
# Crime, Mystery, Action & Adventure, War & Politics: the series a suggestion
# may be. A drama with none of these still qualifies as a legal drama.
CORE_SERIES_GENRES = frozenset({80, 9648, 10759, 10768})
_LEGAL = re.compile(r"\b(?:lawyer|attorney|courtroom|law firm|legal (?:drama|thriller)|prosecutor|"
                    r"public defender|defense attorney|district attorney|corporate law)\b", re.I)
# A TMDB place keyword: "london, england", "los angeles, california", or one of
# these. Places say where a story is set, not what it is about.
_PLACES = frozenset("""
london england scotland wales ireland britain uk usa america europe africa asia australia canada mexico
russia germany france italy spain japan china india iran iraq afghanistan israel colombia brazil
paris berlin moscow rome madrid amsterdam dublin belfast glasgow edinburgh manchester liverpool birmingham
leeds newcastle cornwall yorkshire chicago miami boston philadelphia detroit baltimore atlanta seattle
texas california florida louisiana montana oklahoma georgia arizona nevada
""".split()) | {"northern ireland", "united kingdom", "great britain", "united states", "new york",
                "new york city", "washington d.c.", "washington dc", "los angeles", "las vegas",
                "new orleans", "san francisco", "new jersey", "middle east", "south africa", "new zealand"}


def place_keyword(keyword: Mapping) -> bool:
    name = str(keyword.get("name") or "").strip().lower()
    return ", " in name or name in _PLACES


def keyword_items(detail: Mapping) -> list[Mapping]:
    keywords = detail.get("keywords") or {}
    if isinstance(keywords, Mapping):
        keywords = keywords.get("results") or keywords.get("keywords") or []
    return [k for k in keywords if isinstance(k, Mapping)] if isinstance(keywords, list) else []


def keyword_names(detail: Mapping) -> list[str]:
    return [str(k.get("name") or "") for k in keyword_items(detail)]


def without_place_keywords(fetch: Callable[[str, Mapping | None], Any],
                           dropped: Counter | None = None) -> Callable[[str, Mapping | None], Any]:
    """tmdb_fetch whose details carry no place keywords; dropped counts them by name."""
    lock = threading.Lock()
    def call(path, params=None):
        value = fetch(path, params)
        keywords = value.get("keywords") if isinstance(value, Mapping) else None
        if not isinstance(keywords, Mapping):
            return value
        kept = {}
        for key in ("results", "keywords"):
            if isinstance(keywords.get(key), list):
                places = [k for k in keywords[key] if isinstance(k, Mapping) and place_keyword(k)]
                kept[key] = [k for k in keywords[key] if not (isinstance(k, Mapping) and place_keyword(k))]
                if dropped is not None and places:
                    with lock:
                        dropped.update(str(k.get("name")) for k in places)
        return dict(value, keywords=dict(keywords, **kept))
    return call


def legal_drama(detail: Mapping) -> bool:
    return any(_LEGAL.search(name) for name in keyword_names(detail))


def aired_by_season(detail: Mapping) -> dict[int, int] | None:
    """Regular episodes aired so far per season, from TMDB details; None when unknown.

    Seasons before the latest aired episode count in full, its own season up to
    that episode, and later seasons not at all.
    """
    seasons = {w._int(s.get("season_number")): w._int(s.get("episode_count")) or 0
               for s in detail.get("seasons") or []
               if isinstance(s, Mapping) and (w._int(s.get("season_number")) or 0) > 0}
    last = detail.get("last_episode_to_air") or {}
    season, number = w._int(last.get("season_number")), w._int(last.get("episode_number"))
    if season and season > 0 and number and number > 0:
        aired = {s: count for s, count in seasons.items() if s < season}
        aired[season] = number
        return aired
    return seasons or None


def series_viewing(watched_shows: Iterable[Mapping],
                   watched_episode_history: Iterable[Mapping] = ()) -> dict[str, dict]:
    """Per show IMDb ID: distinct regular episodes watched, episode plays, play times.

    Distinct episodes ignore Trakt's reset_at: a show finished and then reset
    for a rewatch still counts as finished. Times come from the episode history,
    per episode too, and plays of specials (season 0) are only counted.
    """
    viewing, alias_to_imdb = {}, {}
    for entry in watched_shows:
        show = entry.get("show") or {}
        imdb = (show.get("ids") or {}).get("imdb")
        if not isinstance(imdb, str) or not imdb:
            continue
        for alias in _aliases(show.get("ids")):
            alias_to_imdb[alias] = imdb
        state = viewing.setdefault(imdb, {"episodes": set(), "plays": 0, "times": [], "episode_times": {},
                                          "specials": 0, "last": None})
        state["plays"] += int(w._num(entry.get("plays"), 0))
        state["last"] = max(filter(None, (state["last"], _aware(entry.get("last_watched_at")))), default=None)
        for season in entry.get("seasons") or []:
            number = w._int(season.get("number"))
            for episode in season.get("episodes") or []:
                epnum = w._int(episode.get("number"))
                if number and number > 0 and epnum and epnum > 0 and w._num(episode.get("plays"), 1) > 0:
                    state["episodes"].add((number, epnum))
    for event in watched_episode_history:
        show, episode = event.get("show") or {}, event.get("episode") or {}
        imdbs = {alias_to_imdb[a] for a in _aliases(show.get("ids")) if a in alias_to_imdb}
        season, number = w._int(episode.get("season")), w._int(episode.get("number"))
        if len(imdbs) != 1:
            continue
        state = viewing[next(iter(imdbs))]
        if season == 0:
            state["specials"] += 1
            continue
        if not season or season < 1 or not number or number < 1:
            continue
        state["episodes"].add((season, number))
        when = _aware(event.get("watched_at"))
        if when:
            state["times"].append(when)
            state["episode_times"].setdefault((season, number), []).append(when)
            state["last"] = max(filter(None, (state["last"], when)))
    return viewing


def repeat_plays(episode_times: Mapping[Any, Sequence[datetime]], gap: timedelta = REWATCH_GAP) -> tuple[int, int]:
    """Repeat plays of the same episodes: (at least `gap` after the last counted play, sooner)."""
    later = sooner = 0
    for times in episode_times.values():
        counted = None
        for when in sorted(times):
            if counted is None or when - counted >= gap:
                later += counted is not None
                counted = when
            else:
                sooner += 1
    return later, sooner


def binged(times: Iterable[datetime], episodes: int = 3, window: timedelta = timedelta(hours=48),
           gap: timedelta = timedelta(minutes=10)) -> bool:
    """At least `episodes` plays inside `window`, as watched rather than marked.

    Plays under `gap` apart count once: marking a season watched in one go
    stamps every episode with the same moment, which is not a binge.
    """
    plays = []
    for when in sorted(times):
        if not plays or when - plays[-1] >= gap:
            plays.append(when)
    return any(plays[i + episodes - 1] - plays[i] <= window for i in range(len(plays) - episodes + 1))


def viewing_pattern(state: Mapping, aired_seasons: Mapping[int, int] | None, now: datetime,
                    factual: bool = False) -> dict:
    """How a show was watched: factual, finished, most, watching, dropped or unknown.

    Completion counts against the aired episodes of the seasons started only
    (Ste, 27 October 2026): a new season not begun yet does not make a show
    finished so far look less liked, and earlier seasons watched outside Trakt
    do not make one look dropped.
    factual: a documentary, reality, news or talk show, whatever was watched.
    finished: 80% or more of those episodes. most: 40-80%.
    dropped: nothing for 60+ days after under 40%, or after 3 episodes or
    fewer of a show not finished (a show loved but watched outside Trakt
    belongs on the Loved list, which outweighs this). watching: under 40%,
    watched in the last 60 days. unknown: TMDB has no episode count.
    Rewatched: repeat plays a day or more apart of at least half the distinct
    episodes (3 or more); repeats sooner than that are reported, not counted.
    """
    distinct = len(state["episodes"])
    started = sorted({season for season, _ in state["episodes"]})
    aired = sum(aired_seasons.get(season, 0) for season in started) if aired_seasons else None
    completion = min(1., distinct / aired) if aired else None
    idle = max(0, (now - state["last"]).days) if state.get("last") else None
    later, sooner = repeat_plays(state.get("episode_times") or {})
    if factual:
        pattern = "factual"
    elif completion is None:
        pattern = "unknown"
    elif completion >= .8:
        pattern = "finished"
    elif idle is not None and idle >= 60 and (completion < .4 or distinct <= 3):
        pattern = "dropped"
    elif completion >= .4:
        pattern = "most"
    else:
        pattern = "watching"
    return {"pattern": pattern, "completion": None if completion is None else round(completion, 3),
            "episodes_watched": distinct, "episodes_aired": aired or None, "seasons_started": started,
            "episode_plays": state["plays"],
            "repeats_day_apart": later, "repeats_same_day": sooner, "specials_plays": state.get("specials", 0),
            "idle_days": idle, "rewatched": distinct >= 3 and later >= .5 * distinct,
            "binged": binged(state["times"])}


def dismissed_titles(items: Iterable[Mapping], kind: str) -> list[tuple[dict, Any]]:
    """(ids, listed_at) per Not Interested entry of one kind, for the taste profile."""
    titles = []
    for item in items:
        if isinstance(item, Mapping):
            ids = ((item.get("movie") if kind == "movie" else item.get("show")) or {}).get("ids") or {}
            if ids.get("tmdb") or ids.get("imdb"):
                titles.append((dict(ids), item.get("listed_at")))
    return titles


def apply_dismissal_signal(scores: Mapping, details: Sequence[tuple[Mapping, Any]],
                           now: datetime, evidence: float = DISMISS_EVIDENCE,
                           decays: bool = True) -> tuple[dict, dict]:
    """Add listed titles to the profile: Not Interested as a mild dislike by default.

    Each title adds `evidence` (decayed from when it was listed as a watch
    decays, unless decays is False) to its keywords, top three cast and
    directors/creators only, mirroring Watchly's profile factors. Genres,
    countries and eras are never touched, so one listed crime show does not
    move crime as a whole. The watchlist uses WATCHLIST_EVIDENCE, undecayed.
    Returns the adjusted scores and their per-category normalisation.
    """
    adjusted = {category: dict(values) for category, values in scores.items()}
    base = evidence
    for detail, listed_at in details:
        when = _aware(listed_at)
        decay = (max(.1, math.exp(-max(0, (now - when).days) / 30.)) if when else .5) if decays else 1.
        evidence = base * decay
        def add(category, key, factor):
            if key:
                values = adjusted.setdefault(category, {})
                values[key] = values.get(key, 0.) + evidence * factor
        for keyword in w._keywords(detail, profile=True):
            add("keywords", keyword, .7)
        credits = detail.get("credits") or {}
        for position, actor in enumerate(credits.get("cast", [])[:3]):
            if isinstance(actor, Mapping):
                add("cast", actor.get("id"), .9 * .7 ** position)
        crew = ([dict(c, job="Creator") for c in detail.get("created_by", []) if isinstance(c, Mapping)]
                + list(credits.get("crew", [])))
        for person in crew:
            if isinstance(person, Mapping) and str(person.get("job", "")).lower() in ("director", "creator"):
                add("directors", person.get("id"), .9)
    normalised = {}
    for category, values in adjusted.items():
        cap = w.CAPS.get(category)
        if cap:
            values.update({key: max(-cap, min(cap, value)) for key, value in values.items()})
        # Scale against the larger of the old and new maximum: lowering the top
        # keyword must still lower it, not rescale it straight back to 1.
        before = [min(cap, value) if cap else value for value in (scores.get(category) or {}).values()]
        maximum = max(max(values.values(), default=0.), max(before, default=0.))
        normalised[category] = {key: value / maximum if maximum > 0 else value for key, value in values.items()}
    return adjusted, normalised


def _regular_episode(episode: Mapping) -> bool:
    number = w._int(episode.get("number"))
    kind = str(episode.get("episode_type") or "").lower().replace(" ", "_")
    return bool(number and number > 0 and kind not in {
        "special", "extra", "trailer", "teaser", "featurette", "behind_the_scenes",
    })


def derive_episode_progress(
    watched_shows: Iterable[Mapping],
    watched_episode_history: Iterable[Mapping] = (),
) -> dict[str, dict]:
    """Join public watched-show and episode-history payloads using stable IDs.

    Public /users/{user}/watched/shows may omit seasons entirely. We therefore
    support both seasons[].episodes[] on each watched-show entry and paginated
    /users/{user}/history/shows rows with show + episode + watched_at. A reset_at
    boundary is honoured for completion, while the title remains known watched.
    No completion state is guessed from show-level plays or last_watched_at.
    """
    states, alias_to_key = {}, {}
    for entry in watched_shows:
        show = entry.get("show") or {}
        key = _show_key(show)
        if not key:
            continue
        state = states.setdefault(key, {
            "show": dict(show), "watched_episodes": {}, "reset_at": entry.get("reset_at"),
            "last_watched_at": entry.get("last_watched_at"), "history_evidence_count": 0,
        })
        for alias in _aliases(show.get("ids")):
            alias_to_key[alias] = key
        reset = _aware(state["reset_at"])
        for season in entry.get("seasons") or []:
            number = w._int(season.get("number"))
            if not number or number < 1:
                continue
            numbers = state["watched_episodes"].setdefault(number, set())
            for episode in season.get("episodes") or []:
                epnum = w._int(episode.get("number"))
                if not epnum or epnum < 1 or w._num(episode.get("plays"), 1) <= 0:
                    continue
                when = _aware(episode.get("last_watched_at"))
                if reset and (when is None or when < reset):
                    continue
                numbers.add(epnum)
                state["history_evidence_count"] += 1
    for event in watched_episode_history:
        show, episode = event.get("show") or {}, event.get("episode") or {}
        keys = {alias_to_key[a] for a in _aliases(show.get("ids")) if a in alias_to_key}
        if len(keys) != 1:
            continue
        state = states[next(iter(keys))]
        season, number = w._int(episode.get("season")), w._int(episode.get("number"))
        if not season or season < 1 or not number or number < 1:
            continue
        reset, when = _aware(state["reset_at"]), _aware(event.get("watched_at"))
        if reset and (when is None or when < reset):
            continue
        state["watched_episodes"].setdefault(season, set()).add(number)
        state["history_evidence_count"] += 1
    return states


def evaluate_returning(state: Mapping, seasons: Sequence[Mapping], now: datetime) -> dict:
    """Conservatively derive a returning decision from episode-level evidence.

    An old started season must have every regular episode completed. A later
    season must be unstarted, and its regular episode 1 must have an aware
    first_aired timestamp at or before now. Specials and season announcements
    never qualify. A started later season belongs to Continue Watching.
    """
    if _aware(now) is None:
        raise ValueError("now must be timezone-aware")
    now = _aware(now)
    watched = {int(s): set(numbers) for s, numbers in (state.get("watched_episodes") or {}).items()
               if int(s) > 0 and numbers}
    if not watched:
        return {"route": "hidden", "reason": "missing_episode_history", "evidence_incomplete": True}
    baseline = max(watched)
    regular = {w._int(s.get("number")): s for s in seasons if w._int(s.get("number")) and int(s["number"]) > 0}
    previous = regular.get(baseline)
    if not previous:
        return {"route": "hidden", "reason": "missing_baseline_season", "evidence_incomplete": True}
    episodes = {w._int(e.get("number")): e for e in previous.get("episodes") or [] if _regular_episode(e)}
    expected = w._int(previous.get("episode_count"))
    if not episodes or (expected is not None and expected != len(episodes)) or set(episodes) != set(range(1, max(episodes) + 1)):
        return {"route": "hidden", "reason": "incomplete_baseline_metadata", "evidence_incomplete": True}
    # Every known regular episode, including not-yet-aired episodes, must be
    # completed before an old season can count as the finished baseline.
    if not set(episodes).issubset(watched[baseline]):
        aired_missing = [e for n, e in episodes.items() if n not in watched[baseline]
                         and _aware(e.get("first_aired")) is not None
                         and _aware(e.get("first_aired")) <= now]
        return {"route": "continue_watching" if aired_missing else "hidden",
                "reason": "started_season_incomplete", "evidence_incomplete": False}
    # Also retain an earlier started season in Continue Watching if it has
    # known released unwatched episodes; do not conceal incomplete viewing.
    for number, watched_numbers in watched.items():
        if number == baseline:
            continue
        info = regular.get(number)
        if info is None or not info.get("episodes"):
            return {"route": "hidden", "reason": "missing_started_season_metadata", "evidence_incomplete": True}
        for episode in info["episodes"]:
            epnum = w._int(episode.get("number"))
            aired = _aware(episode.get("first_aired"))
            if epnum and epnum > 0 and epnum not in watched_numbers and aired and aired <= now:
                return {"route": "continue_watching", "reason": "earlier_started_season_incomplete", "evidence_incomplete": False}
    premieres, released_by_season = [], {}
    for number, season in regular.items():
        if number <= baseline:
            continue
        episode_one = next((e for e in season.get("episodes") or [] if w._int(e.get("number")) == 1 and _regular_episode(e)), {})
        aired = _aware(episode_one.get("first_aired"))
        premieres.append(SeasonPremiere(number, aired, verified=aired is not None))
        if aired:
            released_by_season[number] = aired
    key = _show_key(state.get("show") or {}) or "show"
    decision = route_title(key, "series", {key: WatchState(True, baseline, frozenset(watched), True)},
                           {key: premieres}, as_of=now)
    result = asdict(decision)
    result.pop("interested", None)
    if decision.route == "returning":
        result["first_episode_released_at"] = released_by_season[decision.season_number].isoformat()
        result["completed_baseline_season"] = baseline
    return result


# These are reusable genre/theme descriptions, never title or ID matches.
_ORG = re.compile(
    r"\b(?:organi[sz]ed crime|gangster(?:s)?|mobster(?:s)?|mafia|mob boss|crime famil(?:y|ies)|"
    r"criminal underworld|crime syndicate|gang war(?:s)?|drug lord(?:s)?|cartel(?:s)?|racketeer\w*|"
    r"gangland|gang leader(?:s)?|drug empire|criminal empire|gang violence|gang member(?:s)?|"
    r"drug traffick\w*|drug deal\w*|street gang(?:s)?|crime lord(?:s)?)\b", re.I)
_ORG_SOFT = re.compile(r"\b(?:gang(?:s)?|underworld|mob|drug trade|criminal organisation|criminal organization)\b", re.I)
_INVESTIGATION = re.compile(
    r"\b(?:detective(?:s)?|investigat\w*|police|procedural|missing person(?:s)?|fugitive(?:s)?|"
    r"bounty hunt\w*|law enforcement|fbi|murder mystery|private eye|serial killer(?:s)?|"
    r"search and rescue|rewards? seek\w*|criminal case(?:s)?)\b", re.I)
_LAW_ROLE = re.compile(
    r"\b(?:detective(?:s)?|police officer(?:s)?|police detective(?:s)?|police department|"
    r"police force|police unit|police task force|task force|law enforcement|"
    r"fbi(?: agent(?:s)?)?|nypd|lapd|undercover (?:cop|officer|agent)(?:s)?|"
    r"police procedural|police corruption|corrupt cop(?:s)?|corrupt police)\b", re.I)
_CRIMINAL_ROLE = re.compile(
    r"\b(?:crime famil(?:y|ies)|criminal famil(?:y|ies)|gangster(?:s)?|mobster(?:s)?|"
    r"mafia|mob boss|crime boss|crime lord(?:s)?|drug lord(?:s)?|drug boss|kingpin(?:s)?|"
    r"drug dealer(?:s)?|criminal for hire|hitm[ae]n|contract killer(?:s)?|"
    r"drug empire|criminal empire|gang leader(?:s)?|street gang(?:s)?|"
    r"bootlegger(?:s)?|money launderer(?:s)?)\b", re.I)


def upcoming_episode(state: Mapping, detail: Mapping, today: date, horizon_days: int) -> dict | None:
    """The next scheduled episode of a show the viewer is caught up on, or None.

    Caught up: TMDB's latest aired episode (last_episode_to_air) is among the
    episodes Trakt history shows as watched. The next episode needs a confirmed
    air date from today to today + horizon_days; ended shows have none.
    """
    upcoming, latest = detail.get("next_episode_to_air"), detail.get("last_episode_to_air")
    if not isinstance(upcoming, Mapping) or not isinstance(latest, Mapping):
        return None
    try:
        airs = date.fromisoformat(str(upcoming.get("air_date") or "")[:10])
    except ValueError:
        return None
    if not today <= airs <= today + timedelta(days=horizon_days):
        return None
    season, number = w._int(latest.get("season_number")), w._int(latest.get("episode_number"))
    if not season or not number or number not in (state.get("watched_episodes") or {}).get(season, ()):
        return None
    return {"air_date": airs.isoformat(), "season_number": w._int(upcoming.get("season_number")),
            "episode_number": w._int(upcoming.get("episode_number")),
            "episode_name": str(upcoming.get("name") or "").strip()}


def recent_release_unwatched(state: Mapping, detail: Mapping, today: date, days: int) -> bool:
    """Cheap pre-check: TMDB's latest aired episode is recent and unwatched."""
    latest = detail.get("last_episode_to_air")
    if not isinstance(latest, Mapping):
        return False
    try:
        aired = date.fromisoformat(str(latest.get("air_date") or "")[:10])
    except ValueError:
        return False
    season, number = w._int(latest.get("season_number")), w._int(latest.get("episode_number"))
    return (today - timedelta(days=days) <= aired <= today and bool(season) and bool(number)
            and number not in (state.get("watched_episodes") or {}).get(season, ()))


def out_now_episodes(state: Mapping, seasons: Sequence[Mapping], now: datetime, days: int) -> dict | None:
    """Episodes out in the last `days` days that a viewer caught up before them hasn't watched.

    Walks aired regular episodes newest first, collecting unwatched ones until
    the first watched episode: that one proves the viewer was caught up before
    this release. An unwatched episode older than the window, or no watched
    episode at all, means they were not, so the show is left out.
    """
    watched = state.get("watched_episodes") or {}
    cutoff = now - timedelta(days=days)
    aired = []
    for season in seasons:
        number = w._int(season.get("number"))
        if not number or number < 1:
            continue
        for episode in season.get("episodes") or []:
            when = _aware(episode.get("first_aired"))
            if _regular_episode(episode) and when is not None and when <= now:
                aired.append((when, number, w._int(episode.get("number")), str(episode.get("title") or "").strip()))
    new = []
    for when, season, number, title in sorted(aired, reverse=True):
        if number in watched.get(season, ()):
            break
        if when < cutoff:
            return None
        new.append((when, season, number, title))
    else:
        return None
    if not new:
        return None
    new.reverse()  # oldest first
    first, last = new[0], new[-1]
    return {"air_date": last[0].date().isoformat(), "season_number": first[1], "episode_number": first[2],
            "last_season_number": last[1], "last_episode_number": last[2], "count": len(new),
            "episode_name": first[3] if len(new) == 1 else "", "out_now": True}


def watchlist_premiere(detail: Mapping, today: date, horizon_days: int, out_now_days: int) -> dict | None:
    """A season premiere of a watchlisted show the viewer hasn't started, or None.

    Due: TMDB's next episode opens a season and is dated from today to today +
    horizon_days. Out now: a season that premiered in the last out_now_days days.
    Mid-season episodes are left out: someone who hasn't started a show starts at
    its beginning, not at next week's episode.
    """
    upcoming = detail.get("next_episode_to_air")
    if isinstance(upcoming, Mapping) and w._int(upcoming.get("episode_number")) == 1:
        try:
            airs = date.fromisoformat(str(upcoming.get("air_date") or "")[:10])
        except ValueError:
            airs = None
        if airs and today <= airs <= today + timedelta(days=horizon_days):
            return {"air_date": airs.isoformat(), "season_number": w._int(upcoming.get("season_number")),
                    "episode_number": 1, "episode_name": str(upcoming.get("name") or "").strip()}
    latest = detail.get("last_episode_to_air")
    for season in detail.get("seasons") or []:
        number = w._int(season.get("season_number"))
        try:
            premiered = date.fromisoformat(str(season.get("air_date") or "")[:10])
        except ValueError:
            continue
        if number and today - timedelta(days=out_now_days) <= premiered <= today:
            last = (w._int(latest.get("episode_number")) if isinstance(latest, Mapping)
                    and w._int(latest.get("season_number")) == number else None) or 1
            return {"air_date": premiered.isoformat(), "season_number": number, "episode_number": 1,
                    "last_season_number": number, "last_episode_number": last, "count": last,
                    "episode_name": "", "out_now": True}
    return None


def _out_now_text(episode: Mapping, today: date) -> tuple[str, str]:
    """(card line, description line), e.g. ("Out now · S6 E6", "S6 E6 · Name · out Wed 21 Oct")."""
    out = date.fromisoformat(episode["air_date"])
    when = "today" if out == today else "yesterday" if out == today - timedelta(days=1) else f"{out:%a} {out.day} {out:%b}"
    season, number = episode["season_number"], episode["episode_number"]
    label = f"S{season} E{number}"
    if episode["count"] > 1:
        label += (f"–{episode['last_episode_number']}" if episode["last_season_number"] == season
                  else f" +{episode['count'] - 1}")
    name = episode.get("episode_name") or ""
    named = label + " · " + name if name and not re.fullmatch(r"Episode \d+", name) else label
    return "Out now · " + label, named + " · out " + when


def _upcoming_text(episode: Mapping, today: date) -> tuple[str, str]:
    """(card line, description line), e.g. ("Wed 21 Oct · S6 E6", "S6 E6 · Name · Wed 21 Oct").

    The card line never carries a four-digit year: Nuvio's Hide Unreleased reads any year
    in releaseInfo later than this one as an unreleased title and drops the card. The
    horizon is 90 days, so the description is the only place that needs the year.
    """
    airs = date.fromisoformat(episode["air_date"])
    when = "Today" if airs == today else "Tomorrow" if airs == today + timedelta(days=1) else \
        f"{airs:%a} {airs.day} {airs:%b}"
    dated = when if airs.year == today.year or when in ("Today", "Tomorrow") else f"{when} {airs.year}"
    season, number = episode.get("season_number"), episode.get("episode_number")
    label = ("Special" if season == 0 else f"S{season} E{number}") if season is not None and number else "Next episode"
    name = episode.get("episode_name") or ""
    named = label + " · " + name if name and not re.fullmatch(r"Episode \d+", name) else label
    return when + " · " + label, named + " · " + dated


def thematic_signals(detail: Mapping) -> dict[str, Any]:
    """Explainable, title-blind signals from TMDB keywords and synopsis."""
    keywords = detail.get("keywords") or {}
    if isinstance(keywords, Mapping):
        keywords = keywords.get("results") or keywords.get("keywords") or []
    names = " | ".join(str(k.get("name") or "") for k in keywords if isinstance(k, Mapping))
    synopsis = str(detail.get("overview") or "")
    org_keywords, org_synopsis = set(_ORG.findall(names)), set(_ORG.findall(synopsis))
    org_soft = set(_ORG_SOFT.findall(names + " | " + synopsis))
    investigation_keywords = set(_INVESTIGATION.findall(names))
    investigation_synopsis = set(_INVESTIGATION.findall(synopsis))
    organised = min(1., .72 * len(org_keywords) + .48 * len(org_synopsis) + .16 * len(org_soft))
    investigation = min(1., .60 * len(investigation_keywords) + .35 * len(investigation_synopsis))
    # Organised-crime is often an antagonist/topic tag on police procedurals.
    # Distinguish who leads the story using synopsis role order; display titles
    # never participate. An explicit criminal role introduced before the police
    # keeps an underworld story in the priority group, even if police appear.
    law_role = _LAW_ROLE.search(synopsis)
    criminal_role = _CRIMINAL_ROLE.search(synopsis)
    strong_law_keywords = bool(re.search(r"\b(?:police procedural|police detective|undercover cop|police corruption)\b", names, re.I))
    investigator_led = bool(
        (law_role and (criminal_role is None or law_role.start() < criminal_role.start()))
        or (strong_law_keywords and criminal_role is None)
    )
    priority_organised = 0. if investigator_led else organised
    # Series identity comes from origin_country. A US-origin show can have an
    # Irish/UK co-production company without being British/Irish gangland TV.
    # Missing origin metadata is conservatively ineligible for the first group.
    countries = {str(c) for c in detail.get("origin_country") or []}
    return {
        "organised_crime": round(organised, 4), "investigation": round(investigation, 4),
        "priority_organised_crime": round(priority_organised, 4),
        "gangster_priority": priority_organised >= .45,
        "investigator_led": investigator_led,
        "narrative_perspective": "investigation" if investigator_led else "criminal_underworld" if priority_organised >= .45 else "broader_crime",
        "first_law_role": law_role.group(0).lower() if law_role else None,
        "first_criminal_role": criminal_role.group(0).lower() if criminal_role else None,
        "british_or_irish": bool(countries.intersection({"GB", "IE"})),
        "matched_organised_crime_terms": sorted(org_keywords | org_synopsis | org_soft),
        "matched_investigation_terms": sorted(investigation_keywords | investigation_synopsis),
    }


def _primary_crime_card(card: Mapping) -> bool:
    theme = card.get("theme") or {}
    return bool(theme.get("gangster_priority") and theme.get("british_or_irish")
                and not theme.get("investigator_led"))


def _primary_prefix_count(cards: Sequence[Mapping]) -> int:
    return min(4, len({c["id"] for c in cards if _primary_crime_card(c)}))


def rank_and_mix_series(cards: Sequence[Mapping], *, limit: int) -> list[dict]:
    """Lead with up to four UK/Irish criminal-led organised-crime stories.

    Then retain two wider gangster choices to one broader crime/investigation
    choice. Each group is ranked by personal score. Returning seasons are added
    afterwards without displacing this primary prefix.
    """
    primary = sorted((dict(c) for c in cards if _primary_crime_card(c)),
                     key=lambda c: (-c["suggested_score"], c.get("id", "")))
    result, seen = [], set()
    for card in primary:
        if card["id"] not in seen:
            result.append(card)
            seen.add(card["id"])
        if len(result) >= min(4, limit):
            break
    gangster = sorted((dict(c) for c in cards if c.get("theme", {}).get("gangster_priority")),
                      key=lambda c: (-c["suggested_score"], c.get("id", "")))
    broader = sorted((dict(c) for c in cards if not c.get("theme", {}).get("gangster_priority")),
                     key=lambda c: (-c["suggested_score"], c.get("id", "")))
    gangster = [c for c in gangster if c["id"] not in seen]
    while len(result) < limit and (gangster or broader):
        for preferred in (gangster, gangster, broader):
            if len(result) >= limit:
                break
            queue = preferred if preferred else (broader if preferred is gangster else gangster)
            while queue:
                card = queue.pop(0)
                if card["id"] not in seen:
                    result.append(card)
                    seen.add(card["id"])
                    break
    return result


# Jenny's theme IDs, from her For You catalogues.
JENNY_TRUE_CRIME_KEYWORD = 33722            # "true crime"
JENNY_MURDER_KEYWORDS = frozenset({207046, 10714})  # "murder mystery", "serial killer"
JENNY_HORROR_KEYWORD = 315058               # "horror"; TMDB TV has no horror genre


def jenny_policy(kind: str) -> w.FilterPolicy:
    """Jenny's gates: her For You quality floors and Ste's English-original rule.

    No genre whitelist. Horror, documentary, fantasy and sci-fi stay eligible:
    TMDB also files horror films under fantasy or sci-fi, and TV horror under
    Sci-Fi & Fantasy. Animation, family, kids, news, reality, soap and talk
    stay out. Like ste_policy, 'en' means the ORIGINAL language.
    """
    if kind not in ("movie", "series"):
        raise ValueError("kind must be movie or series")
    movie = kind == "movie"
    return w.FilterPolicy(
        year_min=None, discovery_style=None,
        excluded_genres=(16, 10751) if movie else (16, 10751, 10762, 10763, 10764, 10766, 10767),
        original_language="en", min_rating=6.0 if movie else 6.5, min_votes=30 if movie else 20,
        released_only=True,
    )


def jenny_theme(detail: Mapping) -> dict[str, float]:
    """Title-blind true crime, murder mystery and horror strengths from TMDB IDs.

    A theme's own keyword (or the horror genre) counts 1. A genre-only hint
    counts .5: a crime documentary for true crime, the mystery genre for murder
    mystery. Raw discover rows carry genres but no keywords, so they only ever
    show the genre part.
    """
    keywords, genres = set(w._keywords(detail, profile=True)), set(w._genres(detail))
    true_crime = 1. if JENNY_TRUE_CRIME_KEYWORD in keywords else .5 if {80, 99} <= genres else 0.
    murder_mystery = 1. if keywords & JENNY_MURDER_KEYWORDS else .5 if 9648 in genres else 0.
    horror = 1. if 27 in genres or JENNY_HORROR_KEYWORD in keywords else 0.
    return {"true_crime": true_crime, "murder_mystery": murder_mystery, "horror": horror,
            "strength": max(true_crime, murder_mystery, horror)}


class SteProfile:
    """Ste's gates, "For Ste" queries and organised-crime weighting (the default).

    Each hook is the code that predates profiles, unchanged, except that films
    released before film_year_min are left out (Ste, 24 October 2026).
    """

    name = "ste"
    # Ste found older films too old to interest him; series keep no lower limit.
    film_year_min = 2000
    # The Trakt watchlist hides suggestions and adds premieres (Ste, 27 October 2026).
    uses_watchlist = True
    # Taste without rating (Ste, 27 October 2026): series evidence from how much
    # of a show he watched, a bonus for binged shows, the "Loved" Trakt list,
    # Trakt's related titles as a candidate source (titles per seed, 0 = off) and
    # Trakt's community score as a second quality floor (None = off).
    series_viewing = True
    binge_bonus = .3
    uses_loved_list = True
    trakt_related_limit = 10
    trakt_rating_minimum = 7.0
    # Series taste from the Loved list, on today's ranking (Ste, 27 October
    # 2026): series may be action, war and politics or legal dramas as well as
    # crime and mystery; place keywords ("london, england") never match; a Not
    # Interested title no longer fades; every Loved show seeds candidates, and a
    # title gains loved_vote_weight for each Loved show (up to loved_vote_cap)
    # whose TMDB recommendations or Trakt related titles include it. The
    # organised-crime weighting, prefix and 2:1 mix stay.
    wider_series_gate = True
    drop_place_keywords = True
    lasting_not_interested = True
    loved_seeds = True
    loved_vote_weight = .10
    loved_vote_cap = 3
    # One Loved show's recommendation list alone is noisy: a title nothing but
    # Loved seeds found needs this many Loved votes (Ste, 27 October 2026).
    loved_only_min_votes = 2
    # Rotten Tomatoes (Ste, 27 October 2026): the best rt_candidates
    # cards of each kind gain up to rt_critic_weight for the Tomatometer and up
    # to rt_audience_weight for the Popcornmeter, scaled from rt_floor (nothing)
    # to 100 (all). A missing score adds nothing.
    rt_kinds = frozenset({"series", "movie"})
    rt_critic_weight = .10
    rt_audience_weight = .10
    rt_floor = 70.
    rt_candidates = 90
    # Kinds whose seeds gain themed history titles and their extra pages.
    themed_kinds = frozenset({"series"})
    themed_source = "organised_crime_history_recommendation"

    def policy(self, kind: str) -> w.FilterPolicy:
        policy = w.ste_policy(kind, newest_first=False)
        if kind == "series" and self.wider_series_gate:
            # Dramas pass here for the legal-drama check after enrichment (final_gate).
            return replace(policy, required_any_genres=tuple(sorted(CORE_SERIES_GENRES | {18})))
        return replace(policy, year_min=self.film_year_min) if kind == "movie" else policy

    def core_candidate(self, raw: Mapping, kind: str) -> bool:
        """Before enrichment: a series candidate with a core genre, not a drama awaiting the legal check."""
        return kind != "series" or not self.wider_series_gate or bool(set(w._genres(raw)) & CORE_SERIES_GENRES)

    def final_gate(self, detail: Mapping, kind: str) -> bool:
        """After enrichment: a core genre, or a drama whose keywords make it a legal drama."""
        return self.core_candidate(detail, kind) or legal_drama(detail)

    def for_you_queries(self, kind: str, now: datetime, pages: int) -> list[dict]:
        return for_you_queries(kind, now, pages)

    def theme(self, detail: Mapping) -> dict:
        return thematic_signals(detail)

    def seed_strength(self, theme: Mapping) -> float | None:
        """Sort key for a themed history seed; None when the title is not themed."""
        return theme["organised_crime"] if theme["gangster_priority"] else None

    def evidence(self, item: Mapping, now: datetime) -> float:
        """Taste evidence for one history item: watched, then watchlist, then ratings.

        Ste rarely rates (27 October 2026), so watching counts most: 1.0 per
        watched title, 2.5 when rewatched, whatever its rating. A series with a
        viewing pattern counts by it instead (VIEWING_EVIDENCE, plus
        REWATCH_BONUS and binge_bonus unless dropped). The watchlist adds
        WATCHLIST_EVIDENCE elsewhere. A title rated but not watched counts
        least: 0.4 loved, 0.3 liked, 0.2 otherwise. Decay is Watchly's. A title
        on the Loved list counts LOVED_EVIDENCE and does not fade.
        """
        if item.get("loved"):
            return LOVED_EVIDENCE
        plays = w._num(item.get("watch_count"), 1)
        viewing = item.get("viewing")
        if viewing:
            base = VIEWING_EVIDENCE[viewing["pattern"]]
            if viewing["pattern"] != "dropped":
                base += REWATCH_BONUS * viewing["rewatched"] + self.binge_bonus * viewing["binged"]
        elif plays >= 1 and w._completion(item) >= .7:
            base = 2.5 if plays >= 2 else 1.
        else:
            bucket = w._bucket(item)
            base = .4 if bucket == "loved" else .3 if bucket == "liked" else .2
        last = w._dt(item.get("last_watched"))
        return base * (max(.1, math.exp(-max(0, (now - last).days) / 30.)) if last else .5)

    def seed_pool(self, items: Sequence[Mapping], kind: str) -> list[dict]:
        """History items Watchly samples seeds from.

        With viewing patterns, Ste's series seeds follow viewing, not old
        ratings: a Loved or rewatched show is "loved", and a dropped or
        rated-only show is no seed.
        """
        if kind != "series" or not self.series_viewing:
            return list(items)
        return [dict(i, rating=None) for i in items
                if i.get("loved") or (i.get("viewing") and i["viewing"]["pattern"] != "dropped")]

    def raw_bonus(self, theme: Mapping, kind: str) -> float:
        return .25 * theme["priority_organised_crime"] + .08 * theme["investigation"] if kind == "series" else 0.

    def score(self, normalised: float, theme: Mapping, kind: str) -> float:
        if kind != "series":
            return normalised
        # 65% actual-history similarity, 25% stated organised-crime
        # preference, 8% investigation, 2% British/Irish tie-break.
        return (.65 * normalised + .25 * theme["priority_organised_crime"]
                + .08 * theme["investigation"] + .02 * int(theme["british_or_irish"]))

    def returner_bonus(self, theme: Mapping) -> float:
        return .25 * theme["priority_organised_crime"]

    def order_series(self, cards: Sequence[Mapping], limit: int) -> tuple[list[dict], int]:
        """Series cards in row order, and the primary prefix length returners sit below."""
        return rank_and_mix_series(cards, limit=limit), _primary_prefix_count(cards)


class JennyProfile(SteProfile):
    """Jenny's broad gates and For You queries, with her three themes boosted.

    Anything her history points to can appear. A true crime, murder mystery or
    horror title gains up to theme_weight; there is no prefix or forced mix.
    """

    name = "jenny"
    # Watchlist rules were asked for on Ste's rows only, and so was taste without rating.
    uses_watchlist = False
    series_viewing = False
    binge_bonus = 0.
    uses_loved_list = False
    trakt_related_limit = 0
    trakt_rating_minimum = None
    wider_series_gate = False
    drop_place_keywords = False
    lasting_not_interested = False
    loved_seeds = False
    loved_vote_weight = 0.
    loved_vote_cap = 0
    loved_only_min_votes = 0
    rt_kinds = frozenset()
    themed_kinds = frozenset({"series", "movie"})
    themed_source = "themed_history_recommendation"
    # Share of a card's score from her themes; the rest is history similarity.
    theme_weight = .25

    def policy(self, kind: str) -> w.FilterPolicy:
        return jenny_policy(kind)

    def for_you_queries(self, kind: str, now: datetime, pages: int) -> list[dict]:
        return jenny_for_you_queries(kind, now, pages)

    def theme(self, detail: Mapping) -> dict:
        return jenny_theme(detail)

    def seed_strength(self, theme: Mapping) -> float | None:
        return theme["strength"] or None

    # Jenny keeps Watchly's own weighting, where ratings count most.
    evidence = staticmethod(w.evidence_weight)

    def raw_bonus(self, theme: Mapping, kind: str) -> float:
        return self.theme_weight * theme["strength"]

    def score(self, normalised: float, theme: Mapping, kind: str) -> float:
        return (1 - self.theme_weight) * normalised + self.theme_weight * theme["strength"]

    def returner_bonus(self, theme: Mapping) -> float:
        return self.theme_weight * theme["strength"]

    def order_series(self, cards: Sequence[Mapping], limit: int) -> tuple[list[dict], int]:
        return sorted((dict(c) for c in cards), key=lambda c: (-c["suggested_score"], c["id"]))[:limit], 0


PROFILES = {profile.name: profile for profile in (SteProfile(), JennyProfile())}


def _canonical_detail(raw: Mapping, detail: Mapping) -> dict:
    merged = dict(raw, **detail)
    merged["genre_ids"] = w._genres(detail) or w._genres(raw)
    # The upstream raw-candidate scorer cannot normally see TV keyword names;
    # this custom stage scores rich candidate metadata consistently with profile.
    merged["keyword_ids"] = w._keywords(detail, profile=True)
    return merged


def _eligible(item: Mapping, policy: w.FilterPolicy, now: datetime) -> bool:
    return not item.get("adult", False) and w.passes_filters(item, policy, now)


def _normalise(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    return [(value - low) / (high - low) if high > low else .5 for value in values]


def _safe_poster(path: Any) -> str | None:
    if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9._/-]+\.(?:jpg|jpeg|png|webp)", path, re.I):
        return "https://image.tmdb.org/t/p/w500" + path
    return None


def _meta(card: Mapping, kind: str) -> dict:
    meta = {
        "id": card["id"], "type": kind, "name": card["title"],
        "poster": card["poster"], "posterShape": "poster",
        "description": card.get("overview") or "", "genres": card.get("genres") or [],
    }
    if card.get("year"):
        meta["releaseInfo"] = str(card["year"])
    if card.get("imdb_id"):
        meta["imdb_id"] = card["imdb_id"]
    if card.get("returning_season"):
        season = card["returning_season"]["season_number"]
        meta["description"] = "Season " + str(season) + " has started.\n\n" + meta["description"]
    if card.get("upcoming_line"):
        # Nuvio shows releaseInfo as given under the poster: the air date, not the year.
        meta["releaseInfo"] = card["upcoming_short"]
        meta["description"] = card["upcoming_line"] + "\n\n" + meta["description"]
    return meta


def generate_catalogues(
    watched_movies: Sequence[Mapping],
    watched_shows: Sequence[Mapping],
    rated_movies: Sequence[Mapping],
    rated_shows: Sequence[Mapping],
    watched_episode_history: Sequence[Mapping],
    tmdb_fetch: Callable[[str, Mapping | None], Any],
    *,
    trakt_fetch: Callable[[str, Mapping | None], Any] | None = None,
    now: datetime | str | None = None,
    config: EngineConfig | None = None,
    dismissed: Sequence[Mapping] | None = None,
    watchlist: Sequence[Mapping] | None = None,
    loved: Sequence[Mapping] | None = None,
    trakt_lookup: Callable[[str, Mapping | None], Any] | None = None,
    rt_lookup: Callable[[str, Any, Any], Mapping | None] | None = None,
) -> dict:
    """Generate three catalogues: suggested series, suggested movies, upcoming episodes.

    Inputs are fresh, fully paginated Trakt payloads (not the tool HTTP envelope).
    tmdb_fetch receives /3-relative paths through the vendored Watchly adapter.
    trakt_fetch is read-only and receives /shows/{id}/seasons with extended=
    episodes,full. Caller-provided caches may serve metadata; watched payloads
    must be refreshed for every published generation.
    dismissed holds the items of the "Not Interested" Trakt list. Those titles
    are hidden exactly like watched ones, and count as a mild dislike of their
    keywords, cast and creators (never genres) in the taste profile.
    watchlist holds the items of the viewer's Trakt watchlist. Those titles are
    left out of the suggestions (the viewer already has them) without counting
    as a dislike, and season premieres of watchlisted shows the viewer hasn't
    started join the upcoming row.
    loved holds the items of the viewer's "Loved" Trakt list: the strongest
    like, which does not fade; unwatched ones are left out of the suggestions.
    trakt_lookup is read-only and receives /shows|movies/{imdb}/related and
    /ratings paths: Trakt's related titles join the candidates, and a title
    Trakt's community scores below the profile's minimum is left out.
    rt_lookup(kind, tmdb_id, imdb_id) returns a title's Rotten Tomatoes
    {"tomatometer", "popcornmeter"} (0-100, None when missing; a runtime may
    stand in other critic and audience scores) or None; the best-scoring
    candidates gain a bounded bonus for high scores.

    Raises GenerationError for missing/insufficient input profile or output.
    Never catch that error by publishing {"metas": []}; retain the old files.
    Diagnostics contain account taste/history facts and should remain private.
    Only result['catalogues'][kind]['payload'] belongs on a public catalogue URL.
    """
    config = config or EngineConfig()
    moment = _aware(now) if now is not None else datetime.now(timezone.utc)
    if moment is None:
        raise ValueError("now must be an ISO timestamp or datetime with a timezone")
    if config.catalogue_limit < 1 or config.minimum_items < 1 or config.minimum_items > config.catalogue_limit:
        raise ValueError("Invalid catalogue/minimum item limit")
    if config.candidate_enrichment_limit < config.catalogue_limit:
        raise ValueError("candidate_enrichment_limit must cover catalogue_limit")
    if config.for_you_pages < 0 or config.freshness_weight < 0 or config.freshness_half_life_days <= 0:
        raise ValueError("Invalid For You page count or freshness weighting")
    if config.upcoming_horizon_days < 0:
        raise ValueError("Invalid upcoming horizon")
    taste = PROFILES.get(config.profile)
    if taste is None:
        raise ValueError("Unknown taste profile")
    for name, value in (("watched_movies", watched_movies), ("watched_shows", watched_shows),
                        ("rated_movies", rated_movies), ("rated_shows", rated_shows),
                        ("watched_episode_history", watched_episode_history)):
        if value is None or not isinstance(value, (list, tuple)):
            raise GenerationError("Missing explicit list input: " + name)
    dismissed = () if dismissed is None else dismissed
    if not isinstance(dismissed, (list, tuple)):
        raise GenerationError("Invalid list input: dismissed")
    watchlist = () if watchlist is None else watchlist
    if not isinstance(watchlist, (list, tuple)):
        raise GenerationError("Invalid list input: watchlist")
    if not taste.uses_watchlist:
        watchlist = ()
    loved = () if loved is None or not taste.uses_loved_list else loved
    if not isinstance(loved, (list, tuple)):
        raise GenerationError("Invalid list input: loved")
    if not watched_movies or not watched_shows:
        raise GenerationError("Both movie and series watched histories are required; retain existing catalogues")
    history = w.history_from_trakt(watched_movies, watched_shows, rated_movies, rated_shows)
    # Place keywords leave every TMDB detail before profile, matching or themes see it.
    places_dropped = Counter()
    fetch = without_place_keywords(tmdb_fetch, places_dropped) if taste.drop_place_keywords else tmdb_fetch
    api = w.callbacks_from_fetch(fetch, language=config.metadata_language)
    result = {"generated_at": moment.isoformat(), "catalogue_order": ["series", "movie", "upcoming"],
              "catalogues": {}, "diagnostics": {"algorithm": "Watchly-based custom Trakt Suggested generator",
                  "watchly_commit": w.COMMIT, "config": asdict(config), "media": {},
                  "input_counts": {"watched_movies": len(watched_movies), "watched_series": len(watched_shows),
                      "rated_movies": len(rated_movies), "rated_series": len(rated_shows),
                      "watched_episode_history": len(watched_episode_history),
                      "not_interested": len(dismissed), "watchlist": len(watchlist), "loved": len(loved)}}}
    upcoming_cards: dict[str, dict] = {}
    watchlist_premieres = 0
    viewing_states = series_viewing(watched_shows, watched_episode_history) if taste.series_viewing else {}
    for kind, watch_rows in (("series", watched_shows), ("movie", watched_movies)):
        media = "tv" if kind == "series" else "movie"
        trakt_path = "/shows/" if kind == "series" else "/movies/"
        policy = taste.policy(kind)
        runner = w.WatchlyPreview(api, policy, moment, config.max_workers)
        runner.evidence = taste.evidence
        items = [dict(i) for i in history if i["type"] == kind]
        if not items:
            raise GenerationError("No usable " + kind + " history IDs", result["diagnostics"])
        loved_imdb, loved_tmdb = dismissed_ids(loved, kind)
        for item in items:
            if item["imdb_id"] in loved_imdb or w._int(item.get("tmdb_id")) in loved_tmdb:
                item["loved"] = True
        viewing_diagnostics = []
        if kind == "series" and viewing_states:
            # The profile reads the same TMDB details from the runner's cache.
            def show_detail(item):
                tid = runner._resolve(item, media)
                return item, tid, runner._details(tid, media) if tid else {}
            for item, tid, detail in runner._map(show_detail, [i for i in items if i["imdb_id"] in viewing_states]):
                if tid:
                    item["tmdb_id"] = tid
                viewing = viewing_pattern(viewing_states[item["imdb_id"]],
                                          aired_by_season(detail) if detail else None, moment,
                                          factual=bool(set(w._genres(detail)) & FACTUAL_GENRES) if detail else False)
                # A show's "plays" are episode plays; only a real rewatch counts as one.
                item.update(viewing=viewing, watch_count=2 if viewing["rewatched"] else 1,
                            completion=viewing["completion"] if viewing["completion"] is not None else 1.)
                factual = viewing["pattern"] == "factual"
                viewing_diagnostics.append(dict(viewing, imdb_id=item["imdb_id"], name=item.get("name"),
                                                loved=bool(item.get("loved")),
                                                evidence=0. if factual else round(taste.evidence(item, moment), 4)))
            # Factual shows have their own rows (Police on Duty) and Suggested
            # never offers them, so they leave the taste profile altogether
            # (Ste, 27 October 2026). They stay watched, so stay hidden.
            factual_ids = {v["imdb_id"] for v in viewing_diagnostics if v["pattern"] == "factual"}
            items = [i for i in items if i["imdb_id"] not in factual_ids]
            loved = [entry for entry in loved
                     if ((entry.get("show") or {}).get("ids") or {}).get("imdb") not in factual_ids]
            if not items:
                raise GenerationError("No usable " + kind + " history IDs", result["diagnostics"])
        profile, normalised, audit = runner._profile(items, media)
        coverage = len(audit["processed"]) / len(items)
        if coverage < config.minimum_profile_coverage:
            raise GenerationError("Insufficient " + kind + " metadata profile coverage", result["diagnostics"])
        dislike_details = []
        for ids, listed_at in dismissed_titles(dismissed, kind):
            tid = w._int(ids.get("tmdb")) or (w._int(runner._call("resolve", ids["imdb"], media)) if ids.get("imdb") else None)
            detail = runner._details(tid, media) if tid else {}
            if detail:
                dislike_details.append((detail, listed_at))
        if dislike_details:
            # A title stays on Not Interested only while it is still not wanted.
            profile, normalised = apply_dismissal_signal(profile, dislike_details, moment,
                                                         decays=not taste.lasting_not_interested)
        like_details = []
        for ids, listed_at in dismissed_titles(watchlist, kind):
            tid = w._int(ids.get("tmdb")) or (w._int(runner._call("resolve", ids["imdb"], media)) if ids.get("imdb") else None)
            detail = runner._details(tid, media) if tid else {}
            if detail:
                like_details.append((detail, listed_at))
        if like_details:
            profile, normalised = apply_dismissal_signal(profile, like_details, moment,
                                                         evidence=WATCHLIST_EVIDENCE, decays=False)
        # Loved titles in the history count through their evidence above; the
        # rest count here, as strongly and without fading.
        history_imdb = {i["imdb_id"] for i in items}
        history_tmdb = {w._int(i.get("tmdb_id")) for i in items} | set(audit["resolved"].values())
        loved_details = []
        for ids, listed_at in dismissed_titles(loved, kind):
            if ids.get("imdb") in history_imdb or w._int(ids.get("tmdb")) in history_tmdb:
                continue
            tid = w._int(ids.get("tmdb")) or (w._int(runner._call("resolve", ids["imdb"], media)) if ids.get("imdb") else None)
            detail = runner._details(tid, media) if tid and tid not in history_tmdb else {}
            if detail:
                loved_details.append((detail, listed_at))
        if loved_details:
            profile, normalised = apply_dismissal_signal(profile, loved_details, moment,
                                                         evidence=LOVED_EVIDENCE, decays=False)
        # Every Loved series, watched or not, seeds candidates.
        loved_tids, loved_seed_imdb, loved_name = [], [], {}
        if kind == "series" and taste.loved_seeds:
            for ids, _ in dismissed_titles(loved, kind):
                tid = w._int(ids.get("tmdb")) or (w._int(runner._call("resolve", ids["imdb"], media)) if ids.get("imdb") else None)
                detail = runner._details(tid, media) if tid else {}
                if detail:
                    name = detail.get("name") or detail.get("title") or str(tid)
                    loved_tids.append(tid)
                    loved_name[tid] = name
                    imdb = ids.get("imdb") or (detail.get("external_ids") or {}).get("imdb_id")
                    if imdb:
                        loved_seed_imdb.append(imdb)
                        loved_name[imdb] = name
        # Candidate TMDB ID -> the Loved shows whose recommendations or Trakt
        # related titles include it: "fans of your Loved shows watch this".
        loved_votes: dict[int, set] = defaultdict(set)
        watched_imdb, watched_tmdb = set(), set()
        for entry in watch_rows:
            ids = (entry.get("movie") or entry.get("show") or {}).get("ids") or {}
            if ids.get("imdb"):
                watched_imdb.add(ids["imdb"])
            if ids.get("tmdb"):
                watched_tmdb.add(int(ids["tmdb"]))
            elif ids.get("imdb") in audit["resolved"]:
                watched_tmdb.add(audit["resolved"][ids["imdb"]])
        detail_for_history = {item["imdb_id"]: runner._detail_cache.get((media, audit["resolved"].get(item["imdb_id"])), {})
                              for item in items}
        seed_pool = taste.seed_pool(items, kind)
        seeds = w.sample_seeds(seed_pool, moment, config.top_seed_count)
        pool_ids = {i["imdb_id"] for i in seed_pool}
        themed_kind = kind in taste.themed_kinds
        strength =({i["imdb_id"]: taste.seed_strength(taste.theme(detail_for_history[i["imdb_id"]]))
                     for i in items} if themed_kind else {})
        if themed_kind:
            themed = [i for i in items if strength[i["imdb_id"]] is not None and i["imdb_id"] in pool_ids
                      and not (i.get("rating") is not None and i["rating"] < 5)]
            themed.sort(key=lambda i: (strength[i["imdb_id"]], w.interest_score(i, moment)), reverse=True)
            seed_ids = {i["imdb_id"] for i in seeds}
            seeds.extend(i for i in themed[:config.thematic_seed_count] if i["imdb_id"] not in seed_ids)
        dismissed_imdb, dismissed_tmdb = dismissed_ids(dismissed, kind)
        dismissed_hits = set()
        listed_imdb, listed_tmdb = dismissed_ids(watchlist, kind)
        watchlist_hits, loved_hits = set(), set()
        candidates, origins = {}, defaultdict(set)
        def merge(rows, origin):
            for raw in rows:
                tid = w._int(raw.get("id"))
                if tid in dismissed_tmdb:
                    dismissed_hits.add(tid)
                elif tid in listed_tmdb:
                    watchlist_hits.add(tid)
                elif tid in loved_tmdb:
                    loved_hits.add(tid)
                elif tid and tid not in watched_tmdb and _eligible(raw, policy, moment):
                    candidates[tid] = dict(raw)
                    origins[tid].add(origin)
        def recommend(seed):
            tid = audit["resolved"].get(seed["imdb_id"])
            return seed, w._rows(runner._call("recommendations", tid, media, 1)) if tid else []
        for seed, rows in runner._map(recommend, seeds):
            merge(rows, "history_recommendation")
        if loved_tids:
            pages = [(tid, page) for tid in loved_tids for page in LOVED_PAGES]
            for (tid, _), rows in runner._map(
                    lambda p: (p, w._rows(runner._call("recommendations", p[0], media, p[1]))), pages):
                merge(rows, LOVED_SOURCE)
                for raw in rows:
                    if w._int(raw.get("id")):
                        loved_votes[w._int(raw["id"])].add(loved_name[tid])
        # Extra pages for metadata-selected themed seeds (Ste: organised crime)
        # prevent a generic popular discovery pool from drowning out that preference.
        if themed_kind:
            thematic_seeds = [i for i in seeds if strength[i["imdb_id"]] is not None]
            thematic_seeds.sort(key=lambda i: (strength[i["imdb_id"]], w.interest_score(i, moment)), reverse=True)
            for seed in thematic_seeds[:config.thematic_seed_count]:
                tid = audit["resolved"].get(seed["imdb_id"])
                if tid:
                    for name, page in (("recommendations", 2), ("recommendations", 3), ("similar", 1)):
                        merge(w._rows(runner._call(name, tid, media, page)), taste.themed_source)
        # Trakt's related titles for the same seeds: a second, Trakt-side view
        # of "more like this". Their TMDB details stand in for a raw row.
        related_found, related_from_history = set(), set()
        if trakt_lookup and taste.trakt_related_limit:
            def trakt_related(seed):
                try:
                    rows = trakt_lookup(trakt_path + seed["imdb_id"] + "/related", {"limit": taste.trakt_related_limit})
                except Exception as error:
                    runner.warnings.append({"stage": "trakt_related", "exception_type": type(error).__name__})
                    return []
                return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []
            seed_imdb = {s["imdb_id"] for s in seeds}
            related_seeds = list(seeds) + [{"imdb_id": imdb} for imdb in loved_seed_imdb if imdb not in seed_imdb]
            for seed, rows in runner._map(lambda s: (s, trakt_related(s)), related_seeds):
                for row in rows:
                    tid = w._int((row.get("ids") or {}).get("tmdb"))
                    if tid:
                        related_found.add(tid)
                        if seed["imdb_id"] in seed_imdb:
                            related_from_history.add(tid)
                        if seed["imdb_id"] in loved_name:
                            loved_votes[tid].add(loved_name[seed["imdb_id"]])
            hidden = watched_tmdb | dismissed_tmdb | listed_tmdb | loved_tmdb
            fresh_ids = sorted(tid for tid in related_found if tid not in candidates and tid not in hidden)
            known = [candidates[tid] for tid in sorted(related_found) if tid in candidates]
            fetched = [dict(_canonical_detail(detail, detail), id=tid)
                       for tid, detail in runner._map(lambda t: (t, runner._details(t, media)), fresh_ids) if detail]
            merge(known + fetched, TRAKT_RELATED_SOURCE)
        queries = runner._discover_queries(profile, media)
        queries = [dict(query, include_adult="false") for query in queries]
        for rows in runner._map(lambda q: w._rows(runner._call("discover", media, q)), queries):
            merge(rows, "history_profile_discovery")
        # The For You rows' own query, as a candidate source only: its date
        # order is kept for diagnostics, never used as a position.
        for_you_rank = {}
        for rows in runner._map(lambda q: w._rows(runner._call("discover", media, q)),
                                taste.for_you_queries(kind, moment, config.for_you_pages)):
            for raw in rows:
                tid = w._int(raw.get("id"))
                if tid and tid not in for_you_rank:
                    for_you_rank[tid] = len(for_you_rank) + 1
            merge(rows, FOR_YOU_SOURCE)
        preliminary = []
        for raw in candidates.values():
            score, _ = w.score_candidate(raw, normalised, media)
            score += taste.raw_bonus(taste.theme(raw), kind)
            preliminary.append((score, raw))
        preliminary.sort(key=lambda p: (-p[0], p[1]["id"]))
        # Reserve enrichment slots for candidates found through thematic seeds,
        # because basic discover rows generally lack keyword metadata.
        selected, selected_ids = [], set()
        thematic_candidates = [r for _, r in preliminary if taste.themed_source in origins[r["id"]]]
        for raw in thematic_candidates[:config.candidate_enrichment_limit // 3] + [r for _, r in preliminary]:
            # Titles found only by For You, Trakt related or Loved seeds are
            # enriched below, outside this budget, so they never displace a
            # history candidate. A drama awaiting the legal check needs a seed.
            if (raw["id"] not in selected_ids and not origins[raw["id"]] <= EXTRA_SOURCES
                    and taste.core_candidate(raw, kind)):
                selected.append(raw)
                selected_ids.add(raw["id"])
            if len(selected) >= config.candidate_enrichment_limit:
                break
        # For You adds at most 20 titles per query page. Enrich all of them on
        # top of the unchanged history pool, so sparse raw discover rows are not
        # dropped before the custom weighting sees their keywords and credits.
        # Trakt related adds at most trakt_related_limit per seed, already detailed.
        # A title only Loved seeds found needs loved_only_min_votes Loved votes.
        loved_only_dropped = []
        for raw in (r for _, r in preliminary if origins[r["id"]] & EXTRA_SOURCES):
            loved_only = (origins[raw["id"]] <= {LOVED_SOURCE, TRAKT_RELATED_SOURCE}
                          and raw["id"] not in related_from_history)
            if loved_only and len(loved_votes.get(raw["id"], ())) < taste.loved_only_min_votes:
                loved_only_dropped.append(raw.get("name") or raw.get("title"))
                continue
            if raw["id"] not in selected_ids and (taste.core_candidate(raw, kind)
                                                  or origins[raw["id"]] & {LOVED_SOURCE, TRAKT_RELATED_SOURCE}):
                selected.append(raw)
                selected_ids.add(raw["id"])
        cards, watched_by_imdb = [], set()
        for raw, detail in runner._map(lambda r: (r, runner._details(r["id"], media)), selected):
            if not detail:
                continue
            combined = _canonical_detail(raw, detail)
            imdb = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
            if not isinstance(imdb, str) or not re.fullmatch(r"tt[0-9]+", imdb):
                continue
            if imdb in watched_imdb:
                watched_by_imdb.add(raw["id"])
                continue
            if imdb in dismissed_imdb:
                dismissed_hits.add(raw["id"])
                continue
            if imdb in listed_imdb:
                watchlist_hits.add(raw["id"])
                continue
            if imdb in loved_imdb:
                loved_hits.add(raw["id"])
                continue
            if not _eligible(combined, policy, moment) or not taste.final_gate(combined, kind):
                continue
            poster = _safe_poster(detail.get("poster_path") or raw.get("poster_path"))
            if not poster:
                continue
            card = runner._card(raw, detail)
            if not card.get("title") or not card.get("id"):
                continue
            score, parts = w.score_candidate(combined, normalised, media)
            card.update(poster=poster, profile_score=score, profile_score_parts=parts,
                        theme=taste.theme(detail), candidate_sources=sorted(origins[raw["id"]]))
            if raw["id"] in for_you_rank:
                card["for_you_rank"] = for_you_rank[raw["id"]]
            cards.append(card)
        profile_values = _normalise([c["profile_score"] for c in cards])
        for card, normalised_score in zip(cards, profile_values):
            card["suggested_score"] = taste.score(normalised_score, card["theme"], kind)
            # Recency replaces For You's pure date order. It depends only on the
            # release date, so every source's titles are treated alike.
            card["freshness"] = round(freshness(card.get("release_date"), moment,
                                                config.freshness_half_life_days), 4)
            card["suggested_score"] += config.freshness_weight * card["freshness"]
            # Loved votes: a bounded bonus per Loved show that points to the title.
            card["loved_votes"] = sorted(loved_votes.get(card.get("tmdb_id"), ()))
            card["suggested_score"] += taste.loved_vote_weight * min(len(card["loved_votes"]), taste.loved_vote_cap)
        # Rotten Tomatoes for the best cards and those the row would show only:
        # lookups are rationed, and a card far below the row cannot climb into
        # it on this bonus alone.
        rt_checked = []
        if rt_lookup and kind in taste.rt_kinds and cards:
            def rt_scores(card):
                try:
                    value = rt_lookup(kind, card.get("tmdb_id"), card.get("imdb_id"))
                except Exception as error:
                    runner.warnings.append({"stage": "rotten_tomatoes", "exception_type": type(error).__name__})
                    value = None
                return card, value if isinstance(value, Mapping) else {}
            best = sorted(cards, key=lambda c: (-c["suggested_score"], c["id"]))[:taste.rt_candidates]
            # The series mix gives places to lower-scored gangster titles, so
            # everything the row would show is looked up as well.
            if kind == "series":
                shown = {c["id"] for c in taste.order_series(cards, config.catalogue_limit)[0]}
                looked = {c["id"] for c in best}
                best += [c for c in cards if c["id"] in shown and c["id"] not in looked]
            for card, value in runner._map(rt_scores, best):
                critics, audience = value.get("tomatometer"), value.get("popcornmeter")
                bonus = (rt_bonus(critics, taste.rt_critic_weight, taste.rt_floor)
                         + rt_bonus(audience, taste.rt_audience_weight, taste.rt_floor))
                card["rotten_tomatoes"] = {"tomatometer": critics, "popcornmeter": audience, "bonus": round(bonus, 4),
                                           "critics_source": value.get("critics_source"),
                                           "audience_source": value.get("audience_source")}
                card["suggested_score"] += bonus
                rt_checked.append(card)
        returning_diagnostics, returners, primary_prefix_count = [], [], 0
        if kind == "series":
            episodes = derive_episode_progress(watched_shows, watched_episode_history)
            for key, state in episodes.items():
                ids = state["show"].get("ids") or {}
                tid = w._int(ids.get("tmdb")) or audit["resolved"].get(ids.get("imdb"))
                if tid in dismissed_tmdb or ids.get("imdb") in dismissed_imdb:
                    dismissed_hits.add(tid or key)
                    returning_diagnostics.append({"show_id": key, "route": "hidden", "reason": "not_interested"})
                    continue
                detail = runner._details(tid, media) if tid else {}
                if (not detail or not _eligible(_canonical_detail(detail, detail), policy, moment)
                        or not taste.final_gate(_canonical_detail(detail, detail), kind)):
                    continue
                if not state["watched_episodes"]:
                    returning_diagnostics.append({"show_id": key, "route": "hidden", "reason": "missing_episode_history"})
                    continue
                trakt_identifier = ids.get("trakt") or ids.get("slug") or ids.get("imdb")
                if not trakt_fetch or not trakt_identifier:
                    returning_diagnostics.append({"show_id": key, "route": "hidden", "reason": "missing_season_metadata_callback"})
                    continue
                try:
                    seasons = trakt_fetch("/shows/" + str(trakt_identifier) + "/seasons", {"extended": "episodes,full"})
                    if not isinstance(seasons, list):
                        raise ValueError("Season response must be a list")
                    decision = evaluate_returning(state, seasons, moment)
                except Exception as error:
                    returning_diagnostics.append({"show_id": key, "route": "hidden", "reason": "season_metadata_failed",
                                                  "exception_type": type(error).__name__})
                    continue
                returning_diagnostics.append(dict(show_id=key, **decision))
                if decision["route"] != "returning":
                    continue
                poster = _safe_poster(detail.get("poster_path"))
                if not poster:
                    continue
                card = runner._card(detail, detail)
                if not isinstance(card.get("imdb_id"), str) or not re.fullmatch(r"tt[0-9]+", card["imdb_id"]):
                    continue
                score, _ = w.score_candidate(_canonical_detail(detail, detail), normalised, media)
                theme = taste.theme(detail)
                age_days = max(0, (moment - _aware(decision["first_episode_released_at"])).days)
                card.update(poster=poster, returning_season=decision, theme=theme,
                            suggested_score=score + taste.returner_bonus(theme) + .15 * math.exp(-age_days / 30))
                returners.append(card)
            # Upcoming Episodes: every caught-up show, outside the taste profile's
            # suggestion filters, from TMDB details the profile has already fetched.
            today = moment.date()
            for key, state in episodes.items():
                ids = state["show"].get("ids") or {}
                tid = w._int(ids.get("tmdb")) or audit["resolved"].get(ids.get("imdb"))
                if not tid or tid in dismissed_tmdb or ids.get("imdb") in dismissed_imdb:
                    continue
                detail = runner._details(tid, media)
                episode = None
                trakt_identifier = ids.get("trakt") or ids.get("slug") or ids.get("imdb")
                if (detail and trakt_fetch and trakt_identifier
                        and recent_release_unwatched(state, detail, today, config.out_now_days)):
                    try:
                        seasons = trakt_fetch("/shows/" + str(trakt_identifier) + "/seasons", {"extended": "episodes,full"})
                        if isinstance(seasons, list):
                            episode = out_now_episodes(state, seasons, moment, config.out_now_days)
                    except Exception:
                        episode = None
                if detail and not episode:
                    episode = upcoming_episode(state, detail, today, config.upcoming_horizon_days)
                poster = _safe_poster(detail.get("poster_path")) if episode else None
                if not poster or detail.get("adult"):
                    continue
                card = runner._card(detail, detail)
                if (not isinstance(card.get("imdb_id"), str) or not re.fullmatch(r"tt[0-9]+", card["imdb_id"])
                        or card["imdb_id"] in dismissed_imdb):
                    continue
                short, line = (_out_now_text if episode.get("out_now") else _upcoming_text)(episode, today)
                card.update(poster=poster, upcoming_episode=episode, upcoming_short=short, upcoming_line=line)
                upcoming_cards.setdefault(card["id"], card)
            # Watchlisted shows not started yet: season premieres only.
            for item in watchlist:
                ids = ((item.get("show") if isinstance(item, Mapping) else None) or {}).get("ids") or {}
                imdb = ids.get("imdb")
                tid = w._int(ids.get("tmdb")) or (w._int(runner._call("resolve", imdb, media)) if imdb else None)
                if (not tid or tid in watched_tmdb or imdb in watched_imdb
                        or tid in dismissed_tmdb or imdb in dismissed_imdb):
                    continue
                detail = runner._details(tid, media)
                episode = (watchlist_premiere(detail, today, config.upcoming_horizon_days,
                                              config.out_now_days) if detail else None)
                poster = _safe_poster(detail.get("poster_path")) if episode else None
                if not poster or detail.get("adult"):
                    continue
                card = runner._card(detail, detail)
                if (not isinstance(card.get("imdb_id"), str) or not re.fullmatch(r"tt[0-9]+", card["imdb_id"])
                        or card["imdb_id"] in dismissed_imdb or card["id"] in upcoming_cards):
                    continue
                short, line = (_out_now_text if episode.get("out_now") else _upcoming_text)(episode, today)
                # A later season is the reminder; someone who hasn't started begins at the start.
                start = " · start at S1 E1" if (episode.get("season_number") or 1) > 1 else ""
                card.update(poster=poster, upcoming_episode=dict(episode, watchlist=True), upcoming_short=short,
                            upcoming_line="On your watchlist · " + line + start)
                upcoming_cards[card["id"]] = card
                watchlist_premieres += 1
            # A show on New & Upcoming has its news there; Suggested keeps its
            # places for new picks, so the row is filled from the next best.
            upcoming_suppressed = sum(c["id"] in upcoming_cards for c in cards + returners)
            cards = [c for c in cards if c["id"] not in upcoming_cards]
            returners = [c for c in returners if c["id"] not in upcoming_cards]
            returners.sort(key=lambda c: (-c["suggested_score"], c["id"]))
        def order(cards):
            if kind != "series":
                return sorted(cards, key=lambda c: (-c["suggested_score"], c["id"]))[:config.catalogue_limit], 0
            mixed, prefix = taste.order_series(cards, config.catalogue_limit)
            for index, card in enumerate(returners[:config.max_returning_items]):
                mixed.insert(min(max(2, prefix) + 4 * index, len(mixed)), card)
            return mixed[:config.catalogue_limit], prefix
        ordered, primary_prefix_count = order(cards)
        # Trakt's community score as a second quality floor, checked only for
        # titles the row would show: one it scores below the minimum (with
        # enough votes) leaves, and the row refills from the next best. Shows
        # being returned to are not checked.
        community, community_dropped = {}, []
        if trakt_lookup and taste.trakt_rating_minimum is not None:
            def community_score(card):
                try:
                    value = trakt_lookup(trakt_path + card["imdb_id"] + "/ratings", None)
                except Exception as error:
                    runner.warnings.append({"stage": "trakt_rating", "exception_type": type(error).__name__})
                    value = None
                return card, value if isinstance(value, Mapping) else None
            while True:
                unchecked = [c for c in ordered if c["id"] not in community and not c.get("returning_season")]
                if not unchecked:
                    break
                low = set()
                for card, value in runner._map(community_score, unchecked):
                    community[card["id"]] = value
                    if value:
                        card["trakt_community"] = {"rating": round(w._num(value.get("rating")), 2),
                                                   "votes": int(w._num(value.get("votes")))}
                        if (card["trakt_community"]["votes"] >= TRAKT_RATING_VOTES
                                and card["trakt_community"]["rating"] < taste.trakt_rating_minimum):
                            low.add(card["id"])
                            community_dropped.append({"id": card["id"], "title": card.get("title"),
                                                      "vote_average": card.get("vote_average"),
                                                      "vote_count": card.get("vote_count"),
                                                      **card["trakt_community"]})
                if not low:
                    break
                cards = [c for c in cards if c["id"] not in low]
                ordered, primary_prefix_count = order(cards)
        # Stable-ID deduplication after every source merge, including returners.
        unique = {}
        for card in ordered:
            unique.setdefault(card["id"], card)
        ordered = list(unique.values())
        diagnostics = {"policy": asdict(policy), "profile_coverage": coverage,
                       "profile_input_count": len(items), "profile_processed_count": len(audit["processed"]),
                       "candidate_count": len(candidates), "enriched_count": len(selected),
                       "eligible_discovery_count": len(cards), "returning_count": len(returners),
                       "on_upcoming_suppressed_count": upcoming_suppressed if kind == "series" else 0,
                       "primary_crime_prefix_count": primary_prefix_count,
                       "for_you": {"returned_count": len(for_you_rank),
                                   "watched_suppressed_count": len(set(for_you_rank) & (watched_tmdb | watched_by_imdb)),
                                   "candidate_count": sum(FOR_YOU_SOURCE in o for o in origins.values()),
                                   "eligible_card_count": sum("for_you_rank" in c for c in cards),
                                   "positions": [{"id": c["id"], "position": i + 1, "for_you_rank": c["for_you_rank"]}
                                                 for i, c in enumerate(ordered) if "for_you_rank" in c]},
                       "not_interested": {"suppressed_count": len(dismissed_hits),
                                          "disliked_profile_titles": len(dislike_details)},
                       "watchlist": {"suppressed_count": len(watchlist_hits),
                                     "liked_profile_titles": len(like_details)},
                       "loved": {"history_titles": sum(bool(i.get("loved")) for i in items),
                                 "liked_profile_titles": len(loved_details), "suppressed_count": len(loved_hits)},
                       "series_viewing": viewing_diagnostics,
                       "seeds": [{"imdb_id": s["imdb_id"], "name": s.get("name")} for s in seeds],
                       "trakt_related": {"found_count": len(related_found),
                                         "candidate_count": sum(TRAKT_RELATED_SOURCE in o for o in origins.values()),
                                         "positions": [{"id": c["id"], "position": i + 1,
                                                        "only_trakt_related": c.get("candidate_sources") == [TRAKT_RELATED_SOURCE]}
                                                       for i, c in enumerate(ordered)
                                                       if TRAKT_RELATED_SOURCE in (c.get("candidate_sources") or [])]},
                       "trakt_rating": {"checked_count": len(community),
                                        "found_count": sum(v is not None for v in community.values()),
                                        "dropped": community_dropped},
                       "loved_seeds": {"titles": sorted(set(loved_name.values())),
                                       "candidate_count": sum(LOVED_SOURCE in o for o in origins.values()),
                                       "voted_count": len(loved_votes),
                                       "loved_only_dropped": sorted(str(t) for t in loved_only_dropped),
                                       "positions": [{"id": c["id"], "position": i + 1, "title": c.get("title"),
                                                      "genres": c.get("genres"), "loved_votes": c.get("loved_votes") or [],
                                                      "organised_crime": (c.get("theme") or {}).get("priority_organised_crime"),
                                                      "legal_drama_only": not bool(set(c.get("genre_ids") or []) & CORE_SERIES_GENRES),
                                                      "sources": c.get("candidate_sources"),
                                                      "returning": bool(c.get("returning_season"))}
                                                     for i, c in enumerate(ordered)] if kind == "series" else [],
                                       "card_scores": sorted(({"title": c.get("title"), "score": round(c["suggested_score"], 4),
                                                               "loved_votes": len(c.get("loved_votes") or []),
                                                               "voters": c.get("loved_votes") or [],
                                                               "sources": c.get("candidate_sources")}
                                                              for c in cards), key=lambda c: -c["score"]) if kind == "series" else []},
                       "rotten_tomatoes": {"checked_count": len(rt_checked),
                                           "tomatometer_count": sum(c["rotten_tomatoes"]["tomatometer"] is not None for c in rt_checked),
                                           "popcornmeter_count": sum(c["rotten_tomatoes"]["popcornmeter"] is not None for c in rt_checked),
                                           "cards": [{"id": c["id"], "title": c.get("title"), **c["rotten_tomatoes"],
                                                      "score": round(c["suggested_score"], 4)} for c in rt_checked]},
                       "returning_decisions": returning_diagnostics,
                       "callback_counts": dict(runner.calls), "warnings": runner.warnings,
                       "selected_cards": ordered}
        result["diagnostics"]["media"][kind] = diagnostics
        if len(ordered) < config.minimum_items:
            raise GenerationError("Too few eligible " + kind + " recommendations; retain the last good output", result["diagnostics"])
        result["catalogues"][kind] = {"id": CATALOGUE_IDS[kind], "name": CATALOGUE_NAMES[kind],
                                      "type": kind, "payload": {"metas": [_meta(c, kind) for c in ordered]}}
    # "Out now" first, newest release first; then upcoming, soonest first.
    # An empty row is valid: nothing caught up is new or due soon.
    out_now = sorted((c for c in upcoming_cards.values() if c["upcoming_episode"].get("out_now")),
                     key=lambda c: (c["upcoming_episode"]["air_date"], c["title"].lower(), c["id"]), reverse=True)
    due = sorted((c for c in upcoming_cards.values() if not c["upcoming_episode"].get("out_now")),
                 key=lambda c: (c["upcoming_episode"]["air_date"], c["title"].lower(), c["id"]))
    upcoming = (out_now + due)[:config.catalogue_limit]
    result["diagnostics"]["media"]["upcoming"] = {
        "horizon_days": config.upcoming_horizon_days, "out_now_days": config.out_now_days,
        "out_now_count": len(out_now), "count": len(upcoming), "watchlist_premieres": watchlist_premieres,
        "selected": [{"id": c["id"], **c["upcoming_episode"]} for c in upcoming]}
    result["diagnostics"]["place_keywords_dropped"] = places_dropped.most_common(60)
    result["catalogues"]["upcoming"] = {"id": CATALOGUE_IDS["upcoming"], "name": CATALOGUE_NAMES["upcoming"],
                                        "type": "series", "payload": {"metas": [_meta(c, "series") for c in upcoming]}}
    return result
