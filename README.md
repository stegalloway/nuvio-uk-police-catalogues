# UK Police Documentaries for Nuvio

Three hand-audited UK factual crime catalogues, hosted on **GitHub Pages**. No Trakt account, monthly connected-app quota, home PC, or running server is required.

| Nuvio tab | Approved programmes (October 2026) |
|---|---:|
| On Duty | 28 |
| Police Investigations | 33 |
| True Crime | 27 |
| **Total** | **88** |

The 56-programme launch list was approved on 15 October 2026; five review-queue programmes and the 27-programme True Crime row were approved on 24 October 2026.

True Crime holds UK documentary series about real cases where the police are not the centre of the story. Police-led programmes stay in On Duty or Police Investigations. True Crime has its own minimum of 10 programmes; the police rows keep 25.

## Install in Nuvio

Once GitHub Pages is active, add this Stremio-compatible manifest URL to Nuvio:

`https://stegalloway.github.io/nuvio-uk-police-catalogues/manifest.json`

In **British TV → Police on Duty**, set the source tabs to the catalogues `On Duty`, `Police Investigations` and `True Crime`. Your existing AIOStreams addon remains responsible for playback and other content; this addon only supplies catalogue posters and IDs.

## Daily automatic refresh

GitHub Actions runs `.github/workflows/daily-refresh.yml` every day at **06:23 UTC**, with manual runs available under Actions → Daily UK police documentary refresh.

1. TMDB is scanned for British-origin documentary/reality TV programmes from the last five years, including Channel 5.
2. Existing shows stay eligible if their first broadcast is within five years, or they have a new original episode within the preceding 24 months. Approved manual exceptions are preserved.
3. Programmes are separated into operational policing and police investigations, deduplicated, and sorted by most recent original episode. Manually retained historical specials/repeats are placed last.
4. New programmes with clear UK police-documentary metadata are automatically added **only after two different-day sightings**. Less-clear results go to `data/review-queue.json` rather than changing the live lists. UK true-crime series that are not police-led are matched separately for the True Crime row under the same rules.
5. The workflow validates and commits the refreshed public `docs/` catalogues and `data/` audit to the repository. If an API or validation step fails, the previous published catalogue stays intact.

### Configuration and credentials

The 88 approved TMDB IDs, age exceptions, and exclusions are in `config/editorial.json`. Newly discovered IDs are tracked in `data/discovered.json` and ambiguous suggestions in `data/review-queue.json`.

A private repository **Actions secret** named `TMDB_ACCESS_TOKEN` grants read-only TMDB API access to GitHub Actions. Never add this value to source code, `docs/`, README, or commit history.

No Trakt token is used. The daily scan reads TMDB only and writes public catalogue metadata; it does not access viewing history or streaming accounts.

## Missed-run watchdog and alerts

GitHub runs scheduled jobs late and sometimes drops them. In October 2026 the 06:23 police
refresh started around 10:50–11:35 UTC, and the first 07:41 Trakt Suggested slot never ran.
`.github/workflows/catalogue-watchdog.yml` runs `scripts/watchdog.py` every hour at :52. It watches
the police refresh, Ste's Trakt Suggested refresh and Jenny's (`trakt-suggested-refresh-jenny.yml`,
hourly at :22; see `trakt_suggested_runtime/README.md`):

- It reads each refresh workflow's own `cron` lines, so changing a schedule needs no watchdog edit.
- If a due time has passed by 25 minutes with no successful or running refresh, it dispatches one.
  If that run fails, it retries once.
- After a second failure for the same due time, or when no refresh has succeeded for longer than the
  schedule allows (police about 27½ h, hourly Trakt Suggested about 4½ h), it opens **one issue per workflow**,
  titled `Catalogue refresh alert: …`, mentioning the repository owner. That gives a GitHub email
  and app notification. A new problem adds a comment; repeats of the same one stay quiet.
- The next successful refresh closes the issue automatically.
- A workflow GitHub disabled for inactivity is re-enabled. One disabled by hand is left alone.
- It uses only the built-in `GITHUB_TOKEN`; no personal token or secret is required.

If a delayed scheduled run arrives after the watchdog has already re-run that slot, the refresh simply
runs twice. Both refreshes are safe to repeat. Check the watchdog's decisions without changing
anything with `python scripts/watchdog.py --dry-run`.

### Optional outside trigger

The watchdog is itself a GitHub schedule. For an independent wake-up, any outside scheduler
can dispatch it hourly. The watchdog decides whether anything is due, so extra calls are harmless.

1. Create a **fine-grained personal access token** for this repository only, with
   **Actions: Read and write**. Note its expiry date.
2. In the outside scheduler (for example cron-job.org), add an hourly `POST` to
   `https://api.github.com/repos/stegalloway/nuvio-uk-police-catalogues/actions/workflows/catalogue-watchdog.yml/dispatches`
   with the headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` and the
   body `{"ref":"main"}`. A successful call returns HTTP 204.

Keep that token only in the scheduler. Never commit it.

## Pages and Stremio routes

GitHub Pages publishes `docs/` from the `main` branch. Important routes:

- `/manifest.json`
- `/catalog/series/on-duty.json`
- `/catalog/series/on-duty/skip=20.json`
- `/catalog/series/police-investigations.json`
- `/catalog/series/police-investigations/skip=20.json`
- `/status.json`

Pages contain up to 20 programmes. Clients advance `skip` by the number actually
returned, and the exact final offset returns `{"metas": []}`. For example, a
28-programme list loads offsets `0 → 20 → 28`, with the last response ending
pagination. Each refresh also rewrites previously published offsets, including
their next pages when a list grows, so cached offsets remain usable as counts
change. The validator follows this same client request sequence before publishing.

If Pages temporarily caches an older version, allow for CDN and Nuvio catalogue caches to expire.

## Source and limitations

Programme and artwork metadata use [TMDB](https://www.themoviedb.org/). This product uses the TMDB API but is **not endorsed or certified by TMDB**. The updater currently scans TMDB; it does **not** independently scrape every UK broadcaster's schedule, and its episode dates depend on TMDB records. Being listed does not guarantee that a stream is available.

All titles were editor-reviewed on 15 October 2026 before initial publication. Daily discovery is deliberately conservative to avoid unrelated US police documentaries, scripted drama, retrospective crime-only documentaries and duplicate TV entries.
