# Trakt Suggested runtime

This package retrieves private recommendation inputs and publishes only two final
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

Run from the repository root:

```sh
python -m trakt_suggested_runtime
python -m trakt_suggested_runtime --validate-only
```

The output is `docs/suggested/manifest.json` plus the two nested catalogue trees.
The addon ID is `org.trakt.suggested.personal`. It supplies catalogue resources
only. Row order and exact labels are `Trakt Suggested Series`, then
`Trakt Suggested Movies`. The existing police manifest and output are untouched.

## Complete inputs and last-good output

Each scheduled generation retrieves full public watched-movie and watched-show
snapshots, fully paginated movie/show ratings, and fully paginated episode
history. A failed list or failed later page is never treated as an empty result.
Pagination headers are checked for changing totals and missing records. The
engine can also request full season/episode release metadata through a read-only
Trakt callback.

The runtime latches callback source failures even if the recommendation engine
catches an individual exception. In that case it refuses to publish. It generates
both rows in a temporary directory and validates them together before promoting
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
in the repository or include it in a commit. `--history-dir PATH` is an explicit
manual/offline option accepting the five filenames declared in `INPUT_FILES`.
The scheduled workflow never uses snapshots. Cache, history and diagnostics paths
under the Pages `docs/` tree are rejected.

## Schedule and scope

The workflow requests a refresh every six hours at 01:41, 07:41, 13:41 and 19:41
UTC, with manual dispatch available. GitHub may start scheduled jobs late; a new
season appears after the next successful refresh and client cache expiry. The
job has a 30-minute timeout and the reader a 25-minute network deadline.

Its concurrency group is shared with the police updater to prevent two updater
jobs pushing the same branch simultaneously. It stages **only `docs/suggested/`**.
It neither changes Trakt watch state nor operates playback, Nuvio appearance,
other add-ons or the police catalogue rules.

Run focused contracts with:

```sh
python -m unittest trakt_suggested_engine.test_engine trakt_suggested_runtime.test_runtime
```
