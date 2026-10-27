# Trakt Suggested recommendation generator

This package generates exactly three precomputed Stremio-compatible catalogues:

1. **Trakt Suggested Series** (`trakt-suggested-series`, type `series`).
2. **Trakt Suggested Movies** (`trakt-suggested-movies`, type `movie`).
3. **New & Upcoming Episodes** (`trakt-upcoming-episodes`, type `series`; see below).

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
    dismissed=not_interested,   # optional: items of the "Not Interested" Trakt list
    watchlist=watchlist,        # optional: items of the Trakt watchlist
    loved=loved,                # optional: items of the "Loved" Trakt list
    trakt_lookup=trakt_lookup,  # optional: callable(path, params=None), see below
)

series_payload = result["catalogues"]["series"]["payload"]
movie_payload = result["catalogues"]["movie"]["payload"]
upcoming_payload = result["catalogues"]["upcoming"]["payload"]  # may be empty
```

TMDB paths are relative to `/3`, e.g. `/tv/123` or `/discover/movie`. The
adapter appends `credits,keywords,external_ids` and requests English-GB metadata.
Each run also sends the two For You Discover queries (see below) through the
same `tmdb_fetch`; no extra credential or URL is needed.
Trakt callbacks only receive `/shows/{trakt_id-or-slug-or-imdb}/seasons` with
`{"extended": "episodes,full"}`. `trakt_lookup` only receives
`/shows|movies/{imdb}/related` with `{"limit": 10}` and `/shows|movies/{imdb}/ratings`
(see "Taste without rating"). Credentials belong in callback request headers
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
  the no-adult rule stay in force. No minimum release year is imposed by these gates; Ste's profile adds a 2000 floor for films (see Taste profiles).
- Actual Trakt watched IDs determine suppression. Ratings inform the profile
  but do not silently mark a rated-only title watched.
- Titles on the "Not Interested" list (`dismissed`) are suppressed at the same
  three points as watched ones: the TMDB candidate pool, the IMDb card check
  and returning-season picks. Movie entries hide films; show, season and
  episode entries hide the whole series.
- Not Interested titles also count as a mild dislike in the taste profile:
  each adds -0.2 evidence (the weight of a title dropped early), fading from
  its `listed_at` date as a watch fades, to its keywords, top three cast and
  directors/creators only. Genres, countries and eras are never touched, so
  dismissing one crime show does not push down crime as a whole. Each row's
  diagnostics report `not_interested.suppressed_count` and
  `not_interested.disliked_profile_titles`.
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
- Every card, from any source, then gains a release-recency term (see
  "For You source and recency").
- Up to four leading suggestions are reserved for British/Irish criminal-led
  organised-crime, gangland and crime-family stories, ranked by the real profile
  and thematic scores. No titles are pinned, and police-led stories do not
  qualify for this leading group merely because they concern organised crime.
  British/Irish identity uses the series' `origin_country` only. Co-production
  countries alone do not qualify a US-origin show; missing origin data does not
  qualify a show for the leading group.
- After that prefix, the mix uses two wider gangster suggestions followed by
  one broader crime/investigation suggestion, repeating while both groups are available.
  An exhausted group is filled from the other group, without duplicate IDs.
- Movies retain history-based relevance order, plus the recency term, after the
  same existing gates.

Thematic weighting uses generic keyword descriptions such as "organized crime",
"crime family", "gangster", "detective" and "missing person". Film/series display
titles are not inspected to determine that weighting.

## For You source and recency

The two "For Ste" TMDB Discover+ catalogues behind the For You Home rows are
reproduced as direct TMDB Discover queries (`for_you_queries`), so one row per
type can carry both. Filters come from the saved Discover+ configuration,
translated exactly as Discover+ `discover()` does:

| Row | TMDB Discover parameters |
| --- | --- |
| Films (`tmdb-d4aecde5-...`) | `/discover/movie`, `sort_by=primary_release_date.desc`, `with_genres=80\|53\|9648`, `without_genres=16\|27\|14\|878\|10751`, `vote_average.gte=7`, `vote_count.gte=100`, `with_original_language=en`, `with_release_type=4`, `primary_release_date.lte=<today UTC>`, `include_adult=false`, `include_video=false`, `include_image_language=en-GB,null`, `language=en-GB` |
| Series (`tmdb-c91b0ae8-...`) | `/discover/tv`, `sort_by=first_air_date.desc`, `with_genres=80\|9648`, `without_genres=16\|99\|10762\|10763\|10764\|10765\|10766\|10767`, `vote_average.gte=7`, `vote_count.gte=30`, `with_original_language=en`, `with_status=0\|3\|4\|5`, `first_air_date.lte=<today UTC>`, `include_adult=false`, `include_image_language=en-GB,null`, `language=en-GB` |

`for_you_pages` (default 2, i.e. 40 titles per type, the depth of the For You
rows) controls how many pages are read; 0 disables the source. On
18 October 2026, page 1 of both queries matched the live Discover+ rows ID for
ID and in the same order.

These titles are candidates only. They pass the same watched-ID suppression
(TMDB ID at merge, IMDb ID after enrichment), policy filters, poster and valid
IMDb `tt` ID rules, and stable-ID deduplication as every other source.
Overlaps keep both source labels. All For You titles are enriched outside the
normal enrichment budget, so they never take a history candidate's slot (at
most 20 extra detail requests per page). They are then scored with the same
profile similarity, series theme weighting,
British/Irish criminal-led prefix and 2:1 gangster/broader mix. No position is
reserved for For You. Its own date order is kept only as a private
`for_you_rank` diagnostic.

Recency replaces For You's pure newest-first order with a bounded bonus. Every
card, whatever its source, gains
`freshness_weight * 0.5 ** (age_days / freshness_half_life_days)`, measured from
the film release or series premiere date (defaults 0.20 and 60 days). A
same-day release gains 0.20, two months 0.10, six months 0.025. Unknown or
future dates gain 0. In the 18 October data, that is about the score gap
between film positions 3 and 20. A well-matched new release can rise into view,
but a weak match cannot overtake a strong history match. Returning seasons keep
their own 30-day recency term. Series recency uses the premiere date, so a new
season of an old show is handled only by the returning-season rule.

## Trakt watchlist

The runtime reads the viewer's public Trakt watchlist (`/users/{user}/watchlist`).
A private or missing watchlist applies none of this; an outage fails the refresh,
like the Not Interested list.

- **Suggestions:** watchlisted titles are left out of Trakt Suggested Series and
  Movies, because the viewer already has them. Unlike Not Interested, they don't
  count as a dislike in the taste profile. Returning-season picks of watched
  shows are unaffected.
- **Taste:** each watchlisted title is also a mild like (+0.5, half a completed
  watch, not fading), added to its keywords, top three cast and directors or
  creators the way Not Interested adds a dislike. Genres are never touched.
- **Evidence order (Ste's profile):** watched counts most (films 1.0, 2.5
  rewatched, whatever the rating; series by viewing pattern, see "Taste without
  rating"), then the watchlist, then titles rated but not watched (0.4 loved,
  0.3 liked, 0.2 otherwise). A title on the Loved list outranks them all. Jenny
  keeps Watchly's weights, where ratings count most.
- **New & Upcoming:** for watchlisted shows the viewer hasn't started, a season
  premiere joins the row: dated within `upcoming_horizon_days`, or out in the
  last `out_now_days`. Mid-season episodes are left out, since an unstarted show
  starts at its beginning. Descriptions start "On your watchlist".

## Taste without rating (Ste, since 27 October 2026)

Ste rarely rates on Trakt, so his profile reads taste from what he already does.
Each part is a `SteProfile` attribute; `JennyProfile` turns every one off, so her
rows are unchanged.

**Series viewing patterns** (`series_viewing`). Trakt's show `plays` count every
episode play, so before this almost every show (39 of 47) counted as rewatched
(2.5). Instead `series_viewing()` collects each show's distinct regular episodes
from the watched snapshot and the episode history (ignoring `reset_at`), and
`viewing_pattern()` compares them with TMDB's aired episodes
(`aired_by_season()`) **of the seasons started only**. A new season not begun
yet does not lower a show finished so far, and earlier seasons watched outside
Trakt do not make it look dropped.

| Pattern | Rule | Evidence |
| --- | --- | --- |
| Finished or caught up | 80%+ of the started seasons' aired episodes | 1.5 |
| Most of it | 40-80% | 1.0 |
| Still watching | under 40%, watched in the last 60 days | 0.6 |
| Dropped | nothing for 60+ days after under 40%, or after 3 episodes or fewer of a show not finished | -0.2 |
| Factual | TMDB documentary, reality, news or talk | left out of the profile |
| Unknown | TMDB has no episode counts | 1.0 |

- **Bonuses, not for dropped shows:** +0.5 for a true rewatch, meaning repeat plays at least a day apart (`REWATCH_GAP`) of at least half the distinct episodes, with 3 or more. +0.3 (`binge_bonus`) for 3 or more plays inside 48 hours, where plays under 10 minutes apart count once, because marking a season watched stamps every episode alike.
- **Same-day repeats** are duplicate scrobbles or restarts. They and plays of specials are reported, not counted.
- **Fading:** the evidence fades from the last play as before.
- **Factual shows** have their own Police on Duty rows, and Suggested Series never offers them, so they carry no taste. They stay watched and hidden.
- **Ratings and seeds:** a show's `watch_count` becomes 2 only for a true rewatch. Series seeds follow viewing, not old ratings: a Loved or rewatched show is "loved", and a dropped or rated-only show is no seed.

**Loved list** (`uses_loved_list`). The items of the viewer's public Trakt list
"Loved" (runtime slug `loved`) are the strongest like: 2.0, not fading, whatever
the title's pattern. Loved overrides "dropped" for a show watched mostly outside
Trakt: Suits, SEAL Team and Jack Ryan had one episode logged each. Loved titles
not in the watched history add 2.0 to their keywords, top three cast and
directors or creators, like the watchlist, and are left out of Suggested,
because the viewer has seen them.

**Trakt related titles** (`trakt_related_limit`, 10 per seed). For every seed,
`/shows|movies/{imdb}/related` adds Trakt's related titles as candidates. Their
TMDB details stand in for a raw row. Like For You titles, they are enriched
outside the history budget and pass every gate. Diagnostics: `trakt_related`.

**Trakt community floor** (`trakt_rating_minimum`, 7.0). A title the row would
show leaves when Trakt users score it under 7.0 with at least 100 votes
(`TRAKT_RATING_VOTES`), and the row refills from the next best. With fewer
votes, TMDB alone decides. Only shown titles are checked: about 60 calls per
row, plus refills. Returning-season picks are not checked. Diagnostics:
`trakt_rating.dropped`.

**Preview, 27 October 2026.** Ste's live history went through the engine as it
ran that day, then with each part alone and with all of them, publishing
nothing.
- **Viewing patterns** across his 47 watched shows: 27 finished, 3 most of it, 1 still watching, 10 dropped and 6 factual.
- **Rewatches:** 3 true rewatches (The Rookie, Slow Horses, Hijack), and about 68 same-day duplicate plays across all shows.
- **Ste's checks on the patterns:**
  - Ted Lasso is a true drop.
  - Reacher, The Boys, Mayor of Kingstown and The Gentlemen come out finished from their latest season.
  - Prisoner and the shows he loves go on the Loved list.
- **Rows:** 16 of 60 series and about 10 films changed. Trakt related added, among others, Ozark, Snowfall, The Americans, Sin City, The Town and Drive. The floor took out mainly 2026 films, whose TMDB scores ran higher than Trakt's.
- **New & Upcoming** was unchanged.
- **The floor:** 7.0 was chosen over 6.8, which would have kept Mutiny, Normal, Shelter, Fuze and Shantaram.

## Taste profiles (Ste and Jenny)

Everything above that names Ste's taste is the `"ste"` profile, the default:
`ste_policy()` gates, the two "For Ste" queries, organised-crime theme signals,
the British/Irish prefix and the 2:1 mix. `EngineConfig(profile="jenny")`
swaps in a second viewer's profile. All of the rest is shared: Watchly history
weighting, watched and Not Interested suppression, the mild dislike, recency,
returning seasons, New & Upcoming, poster and IMDb rules, and pagination.

| | `ste` (default) | `jenny` |
| --- | --- | --- |
| Gates | English originals; films must be crime/thriller/mystery, no horror, released 2000 or later (since 24 October 2026); series crime/mystery, no documentary; at least 7/10 with 100 (films) or 30 (series) votes | English originals, as Ste's; no genre whitelist; no animation or family (films), nor animation, family, kids, news, reality, soap or talk (series); at least 6.0/10 and 30 votes (films), 6.5/10 and 20 votes (series), her For You floors |
| For You queries | 2 "For Ste" queries (`for_you_queries`) | Her 6 Discover+ catalogues (`jenny_for_you_queries`): True Crime (keyword 33722), Murder Mysteries (keywords 207046 or 10714 with genre 9648 or 80) and Horror (genre 27 for films, keyword 315058 for TV), with her own rating and vote floors, plus `with_original_language=en` |
| Theme | Organised crime, investigation, British/Irish (`thematic_signals`) | True crime, murder mystery, horror from TMDB IDs (`jenny_theme`): a theme keyword or the horror genre counts 1, a crime documentary or the mystery genre counts 0.5 |
| Score | Series 65/25/8/2 (above); films history only | Both types: 75% history similarity plus 25% theme strength. A themed title is boosted, an unthemed one still qualifies |
| Order | British/Irish prefix, then 2:1 mix | Score order, with no prefix. Returning seasons from 3rd place, every 4th |
| Themed seeds | Series: up to 4 organised-crime history titles with extra pages | Films and series: up to 4 themed history titles with extra pages |
| Taste without rating | Series viewing patterns, binge bonus, Loved list, Trakt related titles, Trakt 7.0 floor | None: Watchly's weights, no Trakt lookups |

Jenny's queries reproduce `tmdb-catalogues-jenny.json` as Discover+
`discover()` translates it: genres and keywords join with `|` (its default
`any` mode). `imdbOnly` needs no parameter, because every card needs an IMDb ID.
At Ste's request (24 October 2026) she has his language preference. Each query
adds `with_original_language=en`, which her Discover+ rows don't have, and the
gate keeps English originals only.

The A/B on 24 October 2026 used Ste's live snapshots: 85 films, 45 shows, 864
episode plays and 24 Not Interested items, with one shared TMDB cache. The new
code's default profile reproduced the pre-profile engine's 18 public files byte
for byte, with identical cards, scores and TMDB call counts.

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

Returning seasons are merged into the one series row after the primary prefix:
normally positions 5, 9, 13, etc. when all four primary slots are filled, with at
most six. When fewer primary choices exist, insertion begins after that prefix
and at least the first two discovery choices. Returning seasons never get a row
of their own.

## New & Upcoming Episodes

A show on this row is left out of Trakt Suggested Series, including its returning-season
card: the news lives here, and Suggested fills that place with the next best pick.

A separate row with two parts. First, **Out now**: shows with an episode released
in the last `out_now_days` (default 30) that the viewer was caught up before and
hasn't watched yet, newest release first. Then shows the viewer is caught up on
whose next episode has a confirmed date within `upcoming_horizon_days`
(default 90), soonest first.

- Out now: a cheap check first (TMDB's `last_episode_to_air` is recent and
  unwatched), then the show's Trakt seasons (`/shows/{id}/seasons`, one request
  per such show). Walking aired regular episodes newest first, unwatched ones are
  collected until the first watched episode, which proves the viewer was caught
  up. An unwatched episode older than the window, or none watched, leaves the
  show out. Cards read `Out now · S6 E6`, or `Out now · S2 E1–8` for a season
  dropped at once; descriptions add the episode name and `out <day>`.
- This keeps a released episode visible for 30 days even when Nuvio's Continue
  Watching would not show it (for example a show removed from it, which Nuvio
  never brings back until an episode is played or marked).

- Caught up: TMDB's latest aired episode (`last_episode_to_air`) is among the
  episodes watched according to Trakt (watched-show payload plus episode history,
  honouring `reset_at`). A show with an aired, unwatched episode is left out.
- The date is TMDB's `next_episode_to_air`, from the show details the profile
  already fetched, so the row adds no extra requests. Ended shows and episodes
  without a date are left out.
- Ste's suggestion filters (genres, language, ratings) do not apply: every
  caught-up show qualifies. Adult titles, missing posters, invalid IMDb IDs and
  Not Interested shows are excluded.
- The line under each poster (`releaseInfo`, which Nuvio shows as given) is the
  day and episode, e.g. `Fri 14 Nov · S3 E1`, in place of the year. It never
  carries a four-digit year: Nuvio's Hide Unreleased hides any card whose
  `releaseInfo` holds a year later than the current one.
- Each description starts with the episode and day, e.g. `S3 E1 · Fri 14 Nov`
  (`Today`, `Tomorrow`; a real episode name is included; specials say `Special`).
  A date in next year adds the year here, e.g. `S9 E1 · Wed 6 Jan 2027`.
- The row may be empty. It is built from Trakt history, so removing items from
  Nuvio's Continue Watching does not affect it.

## Posters and response shape

All posters are clean TMDB portrait images with `posterShape: "poster"`.
No ratings overlays, episode stills, synthetic placeholder cards or error cards
are emitted. Items without a usable portrait poster or a valid IMDb `tt` ID are
omitted so the existing Nuvio/AIO metadata path can resolve them. Keep this rule.
TMDB splits anthologies such as Monster into one entry per story (e.g.
`tmdb:299939`, The Lizzie Borden Story). Those entries have no IMDb ID on TMDB or
Trakt. IMDb only has the anthology (`tt13207736`), so IMDb-keyed artwork showed
the Dahmer poster on that tile. Never substitute a parent or anthology IMDb ID. Catalogue
descriptions may identify a returning season, while the title and poster stay
those of the series. Streams and playback settings are outside this package.

## Validation

Run from the package's parent directory:

```sh
python3 -m unittest trakt_suggested_engine.test_engine -v
```

The 62 focused tests cover public/nested history adapters, stable-ID joins,
episode release boundaries, announced/future/special content, incomplete
seasons, reset boundaries, started-season exclusion, title-blind theme scoring,
series mixing and British/Irish criminal-led priority, police-led versus criminal-led organised crime, watched-vs-rated
separation, filter/poster rules, exactly two
catalogues, returning-item insertion, valid IMDb-only output and failure instead
of empty output. The For You tests check the exact Discover parameters, entry
into the pool, ranking by weighting rather than source order, the series prefix
and mix, deduplication, both watched-suppression gates, an unchanged history
enrichment budget, inherited filters,
private-field exclusion, disabling the source and the recency curve. The Not
Interested tests check that listed films and shows (including IMDb-only and
episode entries) vanish, a show never hides a film with the same TMDB number,
an empty list changes nothing, and a listed show gets no returning season. Every
end-to-end contract is re-run with For You rows present. The Upcoming tests check
caught-up-only selection, the 90-day horizon, ended shows, Not Interested,
suggestion filters not applying, soonest-first order and an empty row. The Jenny
profile tests check:
- her six exact Discover parameters, and that Ste's query is never sent;
- her gates: horror, documentary series and her rating floors in; non-English originals, animation and adult titles out;
- that theme strengths come from IDs, not titles;
- the 0.25 theme boost, with unthemed titles still eligible;
- score order with no prefix, and returning seasons from 3rd place;
- unchanged Not Interested and upcoming behaviour;
- that the default profile is `ste` and unknown names fail.
