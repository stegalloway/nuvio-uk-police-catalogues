"""Preview-only routing; performs no reads, writes, account edits, or scheduling.

Callers must supply real episode progress, a known highest watched/completed
regular-season baseline, and trusted episode-level premiere metadata. A manual
"watched" override can suppress discovery without inventing Trakt completion.
`caught_up=False` means released, unwatched episodes remain in a started season;
True means none remain as of the progress snapshot; None means unknown. Refresh
progress before each evaluation. A premiere's `verified` flag means its first
regular episode's release is confirmed, not merely a season announcement. Use
timezone-aware release instants; a calendar date alone cannot prove it has aired.
All identity matching uses stable IDs. Interests affect neither genre rules nor
watched suppression. This helper does not implement or install a live catalogue.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Literal, Mapping, Sequence


@dataclass(frozen=True)
class WatchState:
    seen: bool = False
    baseline_season: int | None = None
    started_seasons: frozenset[int] = frozenset()
    caught_up: bool | None = None


@dataclass(frozen=True)
class SeasonPremiere:
    number: int
    first_episode_released_at: datetime | None
    verified: bool = False


@dataclass(frozen=True)
class Decision:
    route: Literal["discovery", "returning", "continue_watching", "hidden"]
    reason: str
    interested: bool
    season_number: int | None = None
    evidence_incomplete: bool = False


def route_title(
    media_id: str,
    media_type: Literal["movie", "series"],
    history: Mapping[str, WatchState],
    premieres: Mapping[str, Sequence[SeasonPremiere]],
    *,
    as_of: datetime,
    interests: Collection[str] = (),
) -> Decision:
    """Route one title, hiding uncertain watched shows until evidence is known."""
    if media_type not in ("movie", "series"):
        raise ValueError("media_type must be movie or series")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    interested = media_id in interests
    state = history.get(media_id, WatchState())
    started = {number for number in state.started_seasons if number > 0}
    baseline = state.baseline_season
    baseline_known = baseline is not None and baseline > 0
    seen = state.seen or bool(started) or baseline_known
    if not seen:
        return Decision("discovery", "unwatched", interested)
    if media_type == "movie":
        return Decision("hidden", "watched_movie", interested)
    if state.caught_up is False and (started or baseline_known):
        return Decision("continue_watching", "started_with_unwatched_episodes", interested)
    if state.caught_up is None or not baseline_known:
        return Decision("hidden", "unknown_episode_progress_or_baseline", interested,
                        evidence_incomplete=True)
    # A started later season cannot be advertised as an unstarted new season.
    highest_watched = max({baseline, *started})
    eligible, uncertain = [], False
    for season in premieres.get(media_id, ()):
        if season.number <= highest_watched or season.number <= 0:
            continue
        released_at = season.first_episode_released_at
        if (not season.verified or released_at is None or released_at.tzinfo is None
                or released_at.utcoffset() is None):
            uncertain = True
            continue
        if released_at <= as_of:
            eligible.append(season.number)
    if eligible:
        return Decision("returning", "new_regular_season_released", interested,
                        season_number=min(eligible))
    return Decision("hidden", "unverified_premiere" if uncertain else "caught_up",
                    interested, evidence_incomplete=uncertain)
