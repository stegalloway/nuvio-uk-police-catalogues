# Trakt Suggested runtime

This package retrieves private recommendation inputs and publishes only three final
Stremio-compatible catalogue rows. Place it next to `trakt_suggested_engine/` at
the repository root. Copy `trakt-suggested-refresh.yml` to
`.github/workflows/trakt-suggested-refresh.yml` when deploying.

## Configuration

Environment variables:

- `TRAKT_USERNAME`: the public Trakt username; the workflow accepts a repository
  secret or variable. No username or viewing history is hardcoded in source.
- `TRAKT_CLIENT_ID`: read API application key, provided as an Actions secret.
- `TMDB_ACCESS_TOKEN`: TMDB bearer credential, as used by the existing police
  updater; alternatively `TMDB_API_KEY` is supported.
- `TRAKT_DISMISS_LIST` (optional): slug of the public Trakt list whose titles are
  never suggested. Default `not-interested`; an empty value turns it off.
- `TRAKT_SUGGESTED_PROFILE` (optional): the engine's taste profile, `ste` or
  `jenny`. Unset or empty keeps the engine default (`ste`); an unknown name
  fails the refresh and keeps the previous catalogues.

## A second viewer (Jenny)

`.github/workflows/trakt-suggested-refresh-jenny.yml` runs the same code for
Jenny with `TRAKT_SUGGESTED_PROFILE=jenny`, `--output docs/suggested-jenny`
and `--addon-id org.trakt.suggested.jenny`. Her manifest is
`/suggested-jenny/manifest.json` on the same Pages site.

- **Secrets:**
  - `JENNY_TRAKT_USERNAME` holds her Trakt **slug**, the username with `-` in place of `_`. The API answers 404 to the username itself.
  - `JENNY_TMDB_API_KEY` holds her TMDB v3 key.
  - `TRAKT_CLIENT_ID` is Ste's Trakt app, shared: public reads need only the client ID.
- **Her Trakt account:**
  - The profile must be public.
  - Her own public list named "Not Interested" works exactly as Ste's does.
  - Nuvio on her device must be connected to her Trakt account, so that it scrobbles and can tick that list.
- **Schedule:**
  - Hourly at :22, in its own concurrency group `trakt-suggested-jenny`.
  - The watchdog checks it at :52 together with Ste's.
  - All three refresh workflows retry `git pull --rebase` and `git push` up to three times, because two groups now push to the branch.
- **Publishing:**
  - It stages only `docs/suggested-jenny/`.
  - Its commits are titled "Refresh Jenny's Trakt Suggested catalogues".
  - Like Ste's, her rows, their commit history and each run's notice (including her Not Interested titles) are public.

## Not Interested list

To dismiss a title, add it to a **public** Trakt list named "Not Interested"
(slug `not-interested`). In Nuvio: long-press **+** on the title's details page,
tick the list, Save. It disappears from every row at the next refresh.
Remove it from the list to let it back in.

The runtime reads the whole list, `/users/{user}/lists/{slug}/items`, fully
paginated. If the list does not exist or is not public (401, 403 or 404 on the
first page), nothing is dismissed and the refresh carries on. The run log then
shows `"not_interested": "no public list named not-interested"`, or the item
count when the list was read. Any other error, including a missing later page,
fails the refresh like every other source. Each run's GitHub notice
lists the titles it found on the list. An outage therefore keeps the
previous catalogues rather than bringing dismissed titles back.

Run from the repository root:

```sh
python -m trakt_suggested_runtime
python -m trakt_suggested_runtime --validate-only
```

The output is `docs/suggested/manifest.json` plus the three nested catalogue trees.
The addon ID is `org.trakt.suggested.personal`. It supplies catalogue resources
only. Row order and exact labels are `Trakt Suggested Series`,
`Trakt Suggested Movies`, then `New & Upcoming Episodes` (type `series`). The first two
must hold at least 5 titles; New & Upcoming Episodes may be empty when nothing a
caught-up show is waiting for airs within 90 days. The existing police manifest
and output are untouched.

Each row also merges the titles from the For You Home rows. The engine sends the
same TMDB Discover queries as the "For Ste" TMDB Discover+ catalogues (two pages
per type) through the existing TMDB client and cache. It never calls the
Discover+ or AIO URLs, and no new secret is needed. Those queries carry today's
date, so their cache entries renew daily. A failed For You request latches like
any other source failure and retains the previous catalogues. The For You rows
in AIO/Nuvio are not changed by this package. Retiring them is a separate
manual step. See `trakt_suggested_engine/README.md` for ranking and recency.

## Complete inputs and last-good output

Each scheduled generation retrieves full public watched-movie and watched-show
snapshots, fully paginated movie/show ratings, and fully paginated episode
history. A failed list or failed later page is never treated as an empty result.
Pagination headers are checked for changing totals and missing records. Trakt's CDN
keeps requests made without a login for up to an hour (`s-maxage=3600`), so every
account read (watched, ratings, history, Not Interested) carries a per-run `_`
parameter and is always fresh. Episode
history is read up to a fixed `end_at` instant, so each run requests its own page
URLs and Trakt cannot mix pages cached at different times. A list whose totals
still change between pages is re-read from page 1, up to three times, before the
refresh fails; the failure names the endpoint and the page totals. The
engine can also request full season/episode release metadata through a read-only
Trakt callback.

The runtime latches callback source failures even if the recommendation engine
catches an individual exception. In that case it refuses to publish. It generates
every row in a temporary directory and validates them together before promoting
the directory. Failed local promotion restores the previous directory. GitHub
publication uses a single commit after generation and validation succeed.

Only final catalogue metadata is permitted in public responses. Extra private
fields, missing posters, credential-bearing artwork URLs, unexpected output
files, duplicate IDs, invalid manifests and missing terminal pagination routes
fail validation. User histories, scores, seed explanations and engine diagnostics
are never placed in the public tree. Public output itself is not private hosting.

## Pagination

Each page contains up to 20 items. Nuvio increments `skip` by the actual item
count returned, so a 28-item catalogue must serve offsets 0, 20 and **28**, with
the last returning `{"metas": []}`. Every previously published offset is retained
and rewritten, together with its subsequent offsets after a list grows. There
are no dummy end-of-list cards. This carries forward the police catalogue fix.

## Cache and manual diagnostics

`--cache-dir PATH` optionally uses a private TMDB cache. Its format matches the
existing comparison cache:

```text
filename = SHA256(path + json.dumps(params, sort_keys=True)) + '.json'
envelope = {path, params, fetchedAt, data}
```

Credentials never form part of cached parameters. Default cache TTL is 12 hours;
`--cache-ttl-hours` changes it. Concurrent identical lookups share a lock and one
request; cache files use unique temporary names and atomic replacement.

`--private-diagnostics PATH` is an explicit local-only option. Never put that file
in the repository or include it in a commit. Per row, it reports For You counts
(`returned`, `watched_suppressed`, `candidate`, `eligible_card`) and each For You
title's final position. The public pages never carry `for_you_rank`, `freshness`
or source labels. `--history-dir PATH` is an explicit
manual/offline option accepting the five filenames declared in `INPUT_FILES`,
plus an optional `trakt-not-interested.json`.
The scheduled workflow never uses snapshots. Cache, history and diagnostics paths
under the Pages `docs/` tree are rejected.

## Schedule and scope

The workflow requests a refresh every hour at :17 UTC, with manual dispatch
available. GitHub often starts this repository's scheduled jobs hours late or
drops them. The watchdog checks at :52 and re-runs any hour with no refresh by
then, so a refresh still happens about once an hour. A new season or a Not
Interested change appears after the next successful refresh, AIO's 5-minute
catalogue cache and a Nuvio restart. The
job has a 30-minute timeout and the reader a 25-minute network deadline.

Its concurrency group is shared with the police updater to prevent two updater
jobs pushing the same branch simultaneously. It stages **only `docs/suggested/`**.
It only reads Trakt, including the Not Interested list. It neither changes Trakt watch state nor operates playback, Nuvio appearance,
other add-ons or the police catalogue rules.

Run focused contracts with:

```sh
python -m unittest trakt_suggested_engine.test_engine trakt_suggested_runtime.test_runtime
```
