# Trakt Suggested recommendation generator

This package generates exactly two precomputed Stremio-compatible catalogues:

1. **Trakt Suggested Series** (`trakt-suggested-series`, type `series`).
2. **Trakt Suggested Movies** (`trakt-suggested-movies`, type `movie`).

It performs no default network requests, credential discovery, filesystem writes,
account mutations, publishing or scheduling. Runtime callbacks own retrieval and
caching; the publisher owns atomic updates and last-good-output retention.

## Integration API

```python
from datetime import datetime, timezone
from trakt_suggested_engine import generate_catalogues, EngineConfig, GenerationError

result = generate_catalogues(
    watched_movies,             # full Trakt watched/movies JSON list
    watched_shows,              # full Trakt watched/shows JSON list
    rated_movies,               # full Trakt ratings/movies JSON list
    rated_shows,                # full Trakt ratings/shows JSON list
    watched_episode_history,    # all pages of public history/shows
    tmdb_fetch,                 # callable(path, params=None) -> decoded JSON
    trakt_fetch=trakt_fetch,    # callable(path, params=None) -> decoded JSON
    now=datetime.now(timezone.utc),
    config=EngineConfig(),
)

series_payload = result["catalogues"]["series"]["payload"]
movie_payload = result["catalogues"]["movie"]["payload"]
```

TMDB paths are relative to `/3`, e.g. `/tv/123` or `/discover/movie`. The
adapter appends `credits,keywords,external_ids` and requests English-GB metadata.
Trakt callbacks only receive `/shows/{trakt_id-or-slug-or-imdb}/seasons` with
`{"extended": "episodes,full"}`. Credentials belong in callback request headers
or URL construction, never in this package or public catalogue JSON.

The five source payloads must be explicit lists. Watched/history payloads must be
fresh and fully paginated for each publication. Ratings may be empty. Metadata
may be cached, but season/episode dates need a refresh policy that lets new
seasons appear promptly. Default settings produce at most 60 items per row and
require at least 5 items and 60% successful history metadata coverage.

The result contains `generated_at`, `catalogue_order`, `catalogues` and
`diagnostics`. Only `catalogues[kind]["payload"]` should be published as a
catalogue response. Diagnostics include profile facts, matching themes and
returning decisions and should remain private.

Catch `GenerationError` by leaving existing public files intact. Never replace
a previously good catalogue with an empty list on an upstream failure. Other
callback failures are recorded as warnings or returning metadata failures; a
runtime may impose a stricter publication gate using its own request error log.

## Ranking and filters

The original Watchly replay is vendored as `watchly_base.py`, pinned to upstream
commit `74f7201232c4437976c647a6f6c268285823c328`. Its history weighting, profile
construction, seed sampling, TMDB discovery queries and candidate scoring form
the basis. This generator deliberately makes the following custom adaptations:

- Exact recovered personal-catalogue filters: English originals; released films
  with any crime/thriller/mystery genre, at least 7/10 and 100 votes; released
  crime/mystery series, at least 7/10 and 30 votes. Existing genre exclusions and
  the no-adult rule stay in force. No minimum release year is imposed.
- Actual Trakt watched IDs determine suppression. Ratings inform the profile
  but do not silently mark a rated-only title watched.
- Series metadata keywords and synopsis supply organised-crime, investigation
  and British/Irish signals. No title names or title IDs are matched or pinned.
- Synopsis role order distinguishes criminal-led underworld/family stories from
  police-led stories about organised crime. Detective/task-force-led and police
  corruption stories stay in the broader group even if they carry an organised
  crime keyword; a crime family introduced before pursuing police stays in the
  priority group. Strong police-procedural keywords support this distinction.
- Additional candidate pages come from the highest-scoring organised-crime
  seeds in actual history. There is no fixed seed title.
- Rich candidate metadata is scored against the history profile. Series order
  uses 65% profile similarity, 25% criminal-led organised-crime preference, 8% investigation
  and a 2% British/Irish tie-break. This does not impose a UK-only filter.
- The leading two suggestions prefer organised crime, followed by one broader
  crime/investigation suggestion, repeating while both groups are available.
  An exhausted group is filled from the other group, without duplicate IDs.
- Movies retain history-based relevance order after the same existing gates.

Thematic weighting uses generic keyword descriptions such as "organized crime",
"crime family", "gangster", "detective" and "missing person". Film/series display
titles are not inspected to determine that weighting.

## Upstream attribution and licence

Watchly is Copyright (c) 2025 Bimal Timilsina and is provided under the MIT
License. This package's Python replay and custom generator derive their
history/profile/recommendation logic from the pinned upstream Watchly source:

- Repository: https://github.com/TimilsinaBimal/Watchly
- Source commit: `74f7201232c4437976c647a6f6c268285823c328`
- Original licence: https://github.com/TimilsinaBimal/Watchly/blob/74f7201232c4437976c647a6f6c268285823c328/LICENSE

The complete original copyright, permission and disclaimer notice is retained
in `LICENSE-Watchly` and must accompany distribution of this package. The local
Python replay and Ste-specific filtering/ranking/returning-season adaptations
are modifications and are not an official Watchly release or hosted service.

## Watched series and later seasons

The public watched-show list does not always include seasons/episodes. The
adapter therefore joins complete episode-history rows using stable Trakt, IMDb
or TMDB IDs; nested watched season/episode payloads are also supported. A
`reset_at` boundary excludes old episode events from completion evidence.

A watched show returns only when:

- The latest started regular season has all its regular episodes watched.
- Earlier started seasons have no known released unwatched episode remaining.
- A later regular season is unstarted.
- Its episode 1 has a timezone-aware Trakt `first_aired` timestamp at or before
  the generation time. A date alone, season announcement, season zero, trailer
  or explicitly special episode does not qualify.

The first started episode of that later season removes the returning suggestion,
leaving further viewing to Continue Watching. Unknown completion or metadata
does not manufacture a returning candidate. No show-specific override exists.

Returning seasons are merged into the one series row at positions 3, 7, 11,
etc., with at most six. The leading two organised-crime suggestions are retained
when enough candidates exist. No third catalogue is generated.

## Posters and response shape

All posters are clean TMDB portrait images with `posterShape: "poster"`.
No ratings overlays, episode stills, synthetic placeholder cards or error cards
are emitted. Items without a usable portrait poster or a valid IMDb `tt` ID are
omitted so the existing Nuvio/AIO metadata path can resolve them. Catalogue
descriptions may identify a returning season, while the title and poster stay
those of the series. Streams and playback settings are outside this package.

## Validation

Run from the package's parent directory:

```sh
python3 -m unittest trakt_suggested_engine.test_engine -v
```

The 20 focused tests cover public/nested history adapters, stable-ID joins,
episode release boundaries, announced/future/special content, incomplete
seasons, reset boundaries, started-season exclusion, title-blind theme scoring,
series mixing, police-led versus criminal-led organised crime, watched-vs-rated
separation, filter/poster rules, exactly two
catalogues, returning-item insertion, valid IMDb-only output and failure instead
of empty output.
