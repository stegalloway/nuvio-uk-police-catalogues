# UK Police Documentaries for Nuvio

Two hand-audited UK police documentary catalogues, hosted on **GitHub Pages**. No Trakt account, monthly connected-app quota, home PC, or running server is required.

| Nuvio tab | Approved October 2026 launch list |
|---|---:|
| On Duty | 28 |
| Police Investigations | 28 |
| **Total** | **56** |

## Install in Nuvio

Once GitHub Pages is active, add this Stremio-compatible manifest URL to Nuvio:

`https://stegalloway.github.io/nuvio-uk-police-catalogues/manifest.json`

In **British TV → Police on Duty**, set the two source tabs to the catalogues `On Duty` and `Police Investigations`. Your existing AIOStreams addon remains responsible for playback and other content; this addon only supplies catalogue posters and IDs.

## Daily automatic refresh

GitHub Actions runs `.github/workflows/daily-refresh.yml` every day at **06:23 UTC**, with manual runs available under Actions → Daily UK police documentary refresh.

1. TMDB is scanned for British-origin documentary/reality TV programmes from the last five years, including Channel 5.
2. Existing shows stay eligible if their first broadcast is within five years, or they have a new original episode within the preceding 24 months. Approved manual exceptions are preserved.
3. Programmes are separated into operational policing and police investigations, deduplicated, and sorted by most recent original episode. Manually retained historical specials/repeats are placed last.
4. New programmes with clear UK police-documentary metadata are automatically added **only after two different-day sightings**. Less-clear results go to `data/review-queue.json` rather than changing the live lists.
5. The workflow validates and commits the refreshed public `docs/` catalogues and `data/` audit to the repository. If an API or validation step fails, the previous published catalogue stays intact.

### Configuration and credentials

The 56 approved TMDB IDs, age exceptions, and exclusions are in `config/editorial.json`. Newly discovered IDs are tracked in `data/discovered.json` and ambiguous suggestions in `data/review-queue.json`.

A private repository **Actions secret** named `TMDB_ACCESS_TOKEN` grants read-only TMDB API access to GitHub Actions. Never add this value to source code, `docs/`, README, or commit history.

No Trakt token is used. The daily scan reads TMDB only and writes public catalogue metadata; it does not access viewing history or streaming accounts.

## Pages and Stremio routes

GitHub Pages publishes `docs/` from the `main` branch. Important routes:

- `/manifest.json`
- `/catalog/series/on-duty.json`
- `/catalog/series/on-duty/skip=20.json`
- `/catalog/series/police-investigations.json`
- `/catalog/series/police-investigations/skip=20.json`
- `/status.json`

If Pages temporarily caches an older version, allow for CDN and Nuvio catalogue caches to expire.

## Source and limitations

Programme and artwork metadata use [TMDB](https://www.themoviedb.org/). This product uses the TMDB API but is **not endorsed or certified by TMDB**. The updater currently scans TMDB; it does **not** independently scrape every UK broadcaster's schedule, and its episode dates depend on TMDB records. Being listed does not guarantee that a stream is available.

All titles were editor-reviewed on 15 October 2026 before initial publication. Daily discovery is deliberately conservative to avoid unrelated US police documentaries, scripted drama, retrospective crime-only documentaries and duplicate TV entries.
