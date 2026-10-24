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

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import math
import re
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
    out_now_days: int = 7
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
                           now: datetime) -> tuple[dict, dict]:
    """Add Not Interested titles to the profile as a mild dislike.

    Each title adds DISMISS_EVIDENCE, decayed from when it was listed as a
    watch decays, to its keywords, top three cast and directors/creators only,
    mirroring Watchly's profile factors. Genres, countries and eras are never
    touched, so dismissing one crime show does not push down crime as a whole.
    Returns the adjusted scores and their per-category normalisation.
    """
    adjusted = {category: dict(values) for category, values in scores.items()}
    for detail, listed_at in details:
        when = _aware(listed_at)
        decay = max(.1, math.exp(-max(0, (now - when).days) / 30.)) if when else .5
        evidence = DISMISS_EVIDENCE * decay
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
        maximum = max(values.values(), default=0.)
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
    # Kinds whose seeds gain themed history titles and their extra pages.
    themed_kinds = frozenset({"series"})
    themed_source = "organised_crime_history_recommendation"

    def policy(self, kind: str) -> w.FilterPolicy:
        policy = w.ste_policy(kind, newest_first=False)
        return replace(policy, year_min=self.film_year_min) if kind == "movie" else policy

    def for_you_queries(self, kind: str, now: datetime, pages: int) -> list[dict]:
        return for_you_queries(kind, now, pages)

    def theme(self, detail: Mapping) -> dict:
        return thematic_signals(detail)

    def seed_strength(self, theme: Mapping) -> float | None:
        """Sort key for a themed history seed; None when the title is not themed."""
        return theme["organised_crime"] if theme["gangster_priority"] else None

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
    if not watched_movies or not watched_shows:
        raise GenerationError("Both movie and series watched histories are required; retain existing catalogues")
    history = w.history_from_trakt(watched_movies, watched_shows, rated_movies, rated_shows)
    api = w.callbacks_from_fetch(tmdb_fetch, language=config.metadata_language)
    result = {"generated_at": moment.isoformat(), "catalogue_order": ["series", "movie", "upcoming"],
              "catalogues": {}, "diagnostics": {"algorithm": "Watchly-based custom Trakt Suggested generator",
                  "watchly_commit": w.COMMIT, "config": asdict(config), "media": {},
                  "input_counts": {"watched_movies": len(watched_movies), "watched_series": len(watched_shows),
                      "rated_movies": len(rated_movies), "rated_series": len(rated_shows),
                      "watched_episode_history": len(watched_episode_history),
                      "not_interested": len(dismissed)}}}
    upcoming_cards: dict[str, dict] = {}
    for kind, watch_rows in (("series", watched_shows), ("movie", watched_movies)):
        media = "tv" if kind == "series" else "movie"
        policy = taste.policy(kind)
        runner = w.WatchlyPreview(api, policy, moment, config.max_workers)
        items = [dict(i) for i in history if i["type"] == kind]
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
            profile, normalised = apply_dismissal_signal(profile, dislike_details, moment)
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
        seeds = w.sample_seeds(items, moment, config.top_seed_count)
        themed_kind = kind in taste.themed_kinds
        strength = ({i["imdb_id"]: taste.seed_strength(taste.theme(detail_for_history[i["imdb_id"]]))
                     for i in items} if themed_kind else {})
        if themed_kind:
            themed = [i for i in items if strength[i["imdb_id"]] is not None
                      and not (i.get("rating") is not None and i["rating"] < 5)]
            themed.sort(key=lambda i: (strength[i["imdb_id"]], w.interest_score(i, moment)), reverse=True)
            seed_ids = {i["imdb_id"] for i in seeds}
            seeds.extend(i for i in themed[:config.thematic_seed_count] if i["imdb_id"] not in seed_ids)
        dismissed_imdb, dismissed_tmdb = dismissed_ids(dismissed, kind)
        dismissed_hits = set()
        candidates, origins = {}, defaultdict(set)
        def merge(rows, origin):
            for raw in rows:
                tid = w._int(raw.get("id"))
                if tid in dismissed_tmdb:
                    dismissed_hits.add(tid)
                elif tid and tid not in watched_tmdb and _eligible(raw, policy, moment):
                    candidates[tid] = dict(raw)
                    origins[tid].add(origin)
        def recommend(seed):
            tid = audit["resolved"].get(seed["imdb_id"])
            return seed, w._rows(runner._call("recommendations", tid, media, 1)) if tid else []
        for seed, rows in runner._map(recommend, seeds):
            merge(rows, "history_recommendation")
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
            # For You-only titles are enriched below, outside this budget, so
            # they never displace a history candidate from enrichment.
            if raw["id"] not in selected_ids and origins[raw["id"]] != {FOR_YOU_SOURCE}:
                selected.append(raw)
                selected_ids.add(raw["id"])
            if len(selected) >= config.candidate_enrichment_limit:
                break
        # For You adds at most 20 titles per query page. Enrich all of them on
        # top of the unchanged history pool, so sparse raw discover rows are not
        # dropped before the custom weighting sees their keywords and credits.
        for raw in (r for _, r in preliminary if FOR_YOU_SOURCE in origins[r["id"]]):
            if raw["id"] not in selected_ids:
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
            if not _eligible(combined, policy, moment):
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
                if not detail or not _eligible(_canonical_detail(detail, detail), policy, moment):
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
            mixed, primary_prefix_count = taste.order_series(cards, config.catalogue_limit)
            returners.sort(key=lambda c: (-c["suggested_score"], c["id"]))
            for index, card in enumerate(returners[:config.max_returning_items]):
                mixed.insert(min(max(2, primary_prefix_count) + 4 * index, len(mixed)), card)
            ordered = mixed[:config.catalogue_limit]
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
        else:
            ordered = sorted(cards, key=lambda c: (-c["suggested_score"], c["id"]))[:config.catalogue_limit]
        # Stable-ID deduplication after every source merge, including returners.
        unique = {}
        for card in ordered:
            unique.setdefault(card["id"], card)
        ordered = list(unique.values())
        diagnostics = {"policy": asdict(policy), "profile_coverage": coverage,
                       "profile_input_count": len(items), "profile_processed_count": len(audit["processed"]),
                       "candidate_count": len(candidates), "enriched_count": len(selected),
                       "eligible_discovery_count": len(cards), "returning_count": len(returners),
                       "primary_crime_prefix_count": primary_prefix_count,
                       "for_you": {"returned_count": len(for_you_rank),
                                   "watched_suppressed_count": len(set(for_you_rank) & (watched_tmdb | watched_by_imdb)),
                                   "candidate_count": sum(FOR_YOU_SOURCE in o for o in origins.values()),
                                   "eligible_card_count": sum("for_you_rank" in c for c in cards),
                                   "positions": [{"id": c["id"], "position": i + 1, "for_you_rank": c["for_you_rank"]}
                                                 for i, c in enumerate(ordered) if "for_you_rank" in c]},
                       "not_interested": {"suppressed_count": len(dismissed_hits),
                                          "disliked_profile_titles": len(dislike_details)},
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
        "out_now_count": len(out_now), "count": len(upcoming),
        "selected": [{"id": c["id"], **c["upcoming_episode"]} for c in upcoming]}
    result["catalogues"]["upcoming"] = {"id": CATALOGUE_IDS["upcoming"], "name": CATALOGUE_NAMES["upcoming"],
                                        "type": "series", "payload": {"metas": [_meta(c, "series") for c in upcoming]}}
    return result
