#!/usr/bin/env python3
"""Preview the proposed True Crime row without publishing anything.

Runs the normal discovery and row building with write=False, then writes a report to
preview/true-crime-report.md (and .json) on the preview branch. docs/ and data/ are untouched.
"""
import concurrent.futures
import json
import os
import pathlib

import update_catalogs as uc

OUT = uc.ROOT / "preview"


def years(tv):
    first = (tv.get("first_air_date") or "")[:4]
    last = (uc.air_date(tv) or uc.date_or_none(tv.get("first_air_date")))
    last = last.isoformat()[:4] if last else ""
    return first if not last or last == first else f"{first}–{last}"


def channel(tv):
    return ", ".join(x.get("name") or "" for x in tv.get("networks") or []) or "?"


def published_ids(slug):
    folder = uc.DOCS / "catalog" / "series" / slug
    metas = []
    for path in sorted(folder.glob("skip=*.json"), key=lambda p: int(p.stem.removeprefix("skip="))):
        metas += json.loads(path.read_text(encoding="utf-8"))["metas"]
    return list(dict.fromkeys(m["name"] for m in metas))


def main():
    editorial = uc.load_json(uc.EDITORIAL)
    discovered = uc.load_json(uc.DISCOVERED, {"schema": 1})
    previous = uc.load_json(uc.REVIEW, {"schema": 1, "candidates": []})
    tmdb = uc.TMDB(os.environ.get("TMDB_ACCESS_TOKEN"))
    ids = list(dict.fromkeys([i for c in editorial["categories"].values() for i in c["tmdbIds"]] +
                             [i for slug in editorial["categories"] for i in discovered.get(slug, [])]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(tmdb.detail, ids))
    seeded = {i: tmdb.cache[i] for i in ids}
    fresh, review, pages, screened = uc.discover(tmdb, editorial, previous, seeded, set(ids))
    current = uc.render_outputs(tmdb, editorial, discovered, fresh, review, pages, screened, write=False)

    row_ids, _ = current["true-crime"]
    seeds = editorial["categories"]["true-crime"]["tmdbIds"]
    dropped = [tmdb.cache[i].get("name") for i in seeds if i not in row_ids]
    tc_review = [c for c in review["candidates"] if c["category"] == "true-crime"]
    police_review = [c for c in review["candidates"] if c["category"] != "true-crime"]

    lines = ["# True Crime row preview", "",
             f"Scanned {pages} TMDB pages; {screened} possible matches checked. Nothing was published.", "",
             f"## True Crime row as it would appear ({len(row_ids)} shows, newest episode first)", "",
             "| # | Programme | Channel | Aired | Episodes | TMDB |", "|---|---|---|---|---|---|"]
    for n, i in enumerate(row_ids, 1):
        tv = tmdb.cache[i]
        lines.append(f"| {n} | {tv.get('name')} | {channel(tv)} | {years(tv)} | {tv.get('number_of_episodes') or '?'} | {i} |")
    if dropped:
        lines += ["", "Starting shows left out (too old or no artwork): " + ", ".join(dropped)]
    lines += ["", f"## New true-crime finds waiting for review ({len(tc_review)})", "",
              "Shows marked auto would be added by themselves after a second daily sighting; the rest need your yes.", "",
              "| # | Programme | Channel | Aired | Episodes | Score | Auto | About |", "|---|---|---|---|---|---|---|---|"]
    for n, c in enumerate(tc_review, 1):
        about = (c.get("overview") or "").replace("|", "/")
        aired = c["firstAir"][:4] if c["lastAir"][:4] == c["firstAir"][:4] else f"{c['firstAir'][:4]}–{c['lastAir'][:4]}"
        lines.append(f"| {n} | {c['name']} | {c.get('network') or '?'} | {aired} | {c.get('episodes') or '?'} | "
                     f"{c['score']} | {'yes' if c['eligibleForAuto'] else 'no'} | {about} |")
    lines += ["", "## Police rows", ""]
    for slug in ("on-duty", "police-investigations"):
        names = [tmdb.cache[i].get("name") for i in current[slug][0]]
        live = published_ids(slug)
        added = [x for x in names if x not in live]
        gone = [x for x in live if x not in names]
        lines.append(f"- {editorial['categories'][slug]['name']}: {len(live)} live -> {len(names)} in preview"
                     + (f"; adds {', '.join(added)}" if added else "") + (f"; drops {', '.join(gone)}" if gone else ""))
    kept = {c["tmdbId"] for c in review["candidates"]}
    left = [c["name"] for c in previous["candidates"] if c["tmdbId"] not in kept]
    lines.append(f"- Police review queue: {len(previous['candidates'])} -> {len(police_review)}"
                 + (f"; leaving it: {', '.join(left)}" if left else ""))

    OUT.mkdir(exist_ok=True)
    (OUT / "true-crime-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "true-crime-report.json").write_text(json.dumps({
        "row": [{"tmdbId": i, "name": tmdb.cache[i].get("name"), "network": channel(tmdb.cache[i]),
                 "aired": years(tmdb.cache[i]), "episodes": tmdb.cache[i].get("number_of_episodes")} for i in row_ids],
        "dropped": dropped, "review": tc_review, "scanPages": pages, "screened": screened},
        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"::notice title=True Crime preview::{len(row_ids)} shows in the row, {len(tc_review)} new finds for review")


if __name__ == "__main__":
    main()
