# Watchly-derived logic: Copyright (c) 2025 Bimal Timilsina (MIT).
# Full upstream notice: LICENSE-Watchly. Local adaptations described in README.md.
"""Two precomputed recommendation catalogues; all I/O is injected by the caller.

This is a Watchly-based custom generator, not an authenticated Watchly client.
The upstream algorithm provides history weighting, profile construction,
candidate discovery and scoring. This module adds Ste's existing catalogue
gates, metadata-based organised-crime priority, a varied series mix, actual
watched-only exclusion and episode-aware reintroduction of later seasons.

No title or title ID is pinned, blocked or treated as a special case. No account
data, keys, files or network are read or written by this module. The caller owns
retrieval, caching, atomic publication and retention of the last good result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import watchly_base as w
from .returning_base import SeasonPremiere, WatchState, route_title


CATALOGUE_IDS = {"series": "trakt-suggested-series", "movie": "trakt-suggested-movies"}
CATALOGUE_NAMES = {"series": "Trakt Suggested Series", "movie": "Trakt Suggested Movies"}


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
) -> dict:
    """Generate exactly two catalogues in series-first order.

    Inputs are fresh, fully paginated Trakt payloads (not the tool HTTP envelope).
    tmdb_fetch receives /3-relative paths through the vendored Watchly adapter.
    trakt_fetch is read-only and receives /shows/{id}/seasons with extended=
    episodes,full. Caller-provided caches may serve metadata; watched payloads
    must be refreshed for every published generation.

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
    for name, value in (("watched_movies", watched_movies), ("watched_shows", watched_shows),
                        ("rated_movies", rated_movies), ("rated_shows", rated_shows),
                        ("watched_episode_history", watched_episode_history)):
        if value is None or not isinstance(value, (list, tuple)):
            raise GenerationError("Missing explicit list input: " + name)
    if not watched_movies or not watched_shows:
        raise GenerationError("Both movie and series watched histories are required; retain existing catalogues")
    history = w.history_from_trakt(watched_movies, watched_shows, rated_movies, rated_shows)
    api = w.callbacks_from_fetch(tmdb_fetch, language=config.metadata_language)
    result = {"generated_at": moment.isoformat(), "catalogue_order": ["series", "movie"],
              "catalogues": {}, "diagnostics": {"algorithm": "Watchly-based custom Trakt Suggested generator",
                  "watchly_commit": w.COMMIT, "config": asdict(config), "media": {},
                  "input_counts": {"watched_movies": len(watched_movies), "watched_series": len(watched_shows),
                      "rated_movies": len(rated_movies), "rated_series": len(rated_shows),
                      "watched_episode_history": len(watched_episode_history)}}}
    for kind, watch_rows in (("series", watched_shows), ("movie", watched_movies)):
        media = "tv" if kind == "series" else "movie"
        policy = w.ste_policy(kind, newest_first=False)
        runner = w.WatchlyPreview(api, policy, moment, config.max_workers)
        items = [dict(i) for i in history if i["type"] == kind]
        if not items:
            raise GenerationError("No usable " + kind + " history IDs", result["diagnostics"])
        profile, normalised, audit = runner._profile(items, media)
        coverage = len(audit["processed"]) / len(items)
        if coverage < config.minimum_profile_coverage:
            raise GenerationError("Insufficient " + kind + " metadata profile coverage", result["diagnostics"])
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
        if kind == "series":
            themed = [i for i in items if thematic_signals(detail_for_history[i["imdb_id"]])["gangster_priority"]
                      and not (i.get("rating") is not None and i["rating"] < 5)]
            themed.sort(key=lambda i: (thematic_signals(detail_for_history[i["imdb_id"]])["organised_crime"],
                                       w.interest_score(i, moment)), reverse=True)
            seed_ids = {i["imdb_id"] for i in seeds}
            seeds.extend(i for i in themed[:config.thematic_seed_count] if i["imdb_id"] not in seed_ids)
        candidates, origins = {}, defaultdict(set)
        def merge(rows, origin):
            for raw in rows:
                tid = w._int(raw.get("id"))
                if tid and tid not in watched_tmdb and _eligible(raw, policy, moment):
                    candidates[tid] = dict(raw)
                    origins[tid].add(origin)
        def recommend(seed):
            tid = audit["resolved"].get(seed["imdb_id"])
            return seed, w._rows(runner._call("recommendations", tid, media, 1)) if tid else []
        for seed, rows in runner._map(recommend, seeds):
            merge(rows, "history_recommendation")
        # Extra pages for metadata-selected organised-crime seeds prevent a
        # generic popular-TV discovery pool from drowning out that preference.
        if kind == "series":
            thematic_seeds = [i for i in seeds if thematic_signals(detail_for_history[i["imdb_id"]])["gangster_priority"]]
            thematic_seeds.sort(key=lambda i: (thematic_signals(detail_for_history[i["imdb_id"]])["organised_crime"],
                                               w.interest_score(i, moment)), reverse=True)
            for seed in thematic_seeds[:config.thematic_seed_count]:
                tid = audit["resolved"].get(seed["imdb_id"])
                if tid:
                    for name, page in (("recommendations", 2), ("recommendations", 3), ("similar", 1)):
                        merge(w._rows(runner._call(name, tid, media, page)), "organised_crime_history_recommendation")
        queries = runner._discover_queries(profile, media)
        queries = [dict(query, include_adult="false") for query in queries]
        for rows in runner._map(lambda q: w._rows(runner._call("discover", media, q)), queries):
            merge(rows, "history_profile_discovery")
        preliminary = []
        for raw in candidates.values():
            score, _ = w.score_candidate(raw, normalised, media)
            theme = thematic_signals(raw)
            if kind == "series":
                score += .25 * theme["priority_organised_crime"] + .08 * theme["investigation"]
            preliminary.append((score, raw))
        preliminary.sort(key=lambda p: (-p[0], p[1]["id"]))
        # Reserve enrichment slots for candidates found through thematic seeds,
        # because basic discover rows generally lack keyword metadata.
        selected, selected_ids = [], set()
        thematic_candidates = [r for _, r in preliminary if "organised_crime_history_recommendation" in origins[r["id"]]]
        for raw in thematic_candidates[:config.candidate_enrichment_limit // 3] + [r for _, r in preliminary]:
            if raw["id"] not in selected_ids:
                selected.append(raw)
                selected_ids.add(raw["id"])
            if len(selected) >= config.candidate_enrichment_limit:
                break
        cards = []
        for raw, detail in runner._map(lambda r: (r, runner._details(r["id"], media)), selected):
            if not detail:
                continue
            combined = _canonical_detail(raw, detail)
            imdb = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
            if not isinstance(imdb, str) or not re.fullmatch(r"tt[0-9]+", imdb):
                continue
            if imdb in watched_imdb or not _eligible(combined, policy, moment):
                continue
            poster = _safe_poster(detail.get("poster_path") or raw.get("poster_path"))
            if not poster:
                continue
            card = runner._card(raw, detail)
            if not card.get("title") or not card.get("id"):
                continue
            score, parts = w.score_candidate(combined, normalised, media)
            card.update(poster=poster, profile_score=score, profile_score_parts=parts,
                        theme=thematic_signals(detail), candidate_sources=sorted(origins[raw["id"]]))
            cards.append(card)
        profile_values = _normalise([c["profile_score"] for c in cards])
        for card, normalised_score in zip(cards, profile_values):
            theme = card["theme"]
            if kind == "series":
                # 65% actual-history similarity, 25% stated organised-crime
                # preference, 8% investigation, 2% British/Irish tie-break.
                card["suggested_score"] = (.65 * normalised_score + .25 * theme["priority_organised_crime"]
                    + .08 * theme["investigation"] + .02 * int(theme["british_or_irish"]))
            else:
                card["suggested_score"] = normalised_score
        returning_diagnostics, returners, primary_prefix_count = [], [], 0
        if kind == "series":
            episodes = derive_episode_progress(watched_shows, watched_episode_history)
            for key, state in episodes.items():
                ids = state["show"].get("ids") or {}
                tid = w._int(ids.get("tmdb")) or audit["resolved"].get(ids.get("imdb"))
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
                theme = thematic_signals(detail)
                age_days = max(0, (moment - _aware(decision["first_episode_released_at"])).days)
                card.update(poster=poster, returning_season=decision, theme=theme,
                            suggested_score=score + .25 * theme["priority_organised_crime"] + .15 * math.exp(-age_days / 30))
                returners.append(card)
            mixed = rank_and_mix_series(cards, limit=config.catalogue_limit)
            primary_prefix_count = _primary_prefix_count(cards)
            returners.sort(key=lambda c: (-c["suggested_score"], c["id"]))
            for index, card in enumerate(returners[:config.max_returning_items]):
                mixed.insert(min(max(2, primary_prefix_count) + 4 * index, len(mixed)), card)
            ordered = mixed[:config.catalogue_limit]
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
                       "returning_decisions": returning_diagnostics,
                       "callback_counts": dict(runner.calls), "warnings": runner.warnings,
                       "selected_cards": ordered}
        result["diagnostics"]["media"][kind] = diagnostics
        if len(ordered) < config.minimum_items:
            raise GenerationError("Too few eligible " + kind + " recommendations; retain the last good output", result["diagnostics"])
        result["catalogues"][kind] = {"id": CATALOGUE_IDS[kind], "name": CATALOGUE_NAMES[kind],
                                      "type": kind, "payload": {"metas": [_meta(c, kind) for c in ordered]}}
    return result
