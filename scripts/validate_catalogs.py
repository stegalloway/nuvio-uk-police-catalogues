#!/usr/bin/env python3
"""Read-only checks for the published Stremio catalogue, including pagination."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
read=lambda p:json.loads(p.read_text(encoding="utf-8"))
editor=read(ROOT/"config"/"editorial.json")
manifest=read(ROOT/"docs"/"manifest.json")
status=read(ROOT/"docs"/"status.json")
assert manifest["id"]=="uk.curated.policedocumentaries"
assert manifest["types"]==["series"]
assert manifest["resources"]==["catalog"]
assert {c["id"] for c in manifest["catalogs"]}=={"on-duty","police-investigations"}
total_ids=set()
for slug,cfg in editor["categories"].items():
    expected=status["counts"][slug]
    folder=ROOT/"docs"/"catalog"/"series"
    first=read(folder/(slug+".json"))["metas"]
    same=read(folder/slug/"skip=0.json")["metas"]
    assert first==same and len(first)<=20
    # Follow Nuvio's actual offsets rather than assuming full 20-item pages.
    # A partial final page still needs a successful empty response afterwards.
    meta=[]
    skip=0
    requested=[]
    while True:
        requested.append(skip)
        path=folder/slug/(f"skip={skip}.json")
        assert path.is_file(),(slug,"Missing pagination response",skip)
        page=read(path)["metas"]
        assert isinstance(page,list) and len(page)<=20,(slug,skip,"Invalid page")
        if not page:break
        meta+=page
        skip+=len(page)
        assert skip<=expected,(slug,"Pagination did not terminate",skip,expected)
    assert len(meta)==expected,(slug,len(meta),expected)
    for path in (folder/slug).glob("skip=*.json"):
        offset=int(path.stem.removeprefix("skip="))
        assert read(path)["metas"]==meta[offset:offset+20],(slug,"Stale page",offset)
    assert len({m["id"] for m in meta})==expected
    assert len({m["name"].casefold() for m in meta})==expected
    assert all(m["id"].startswith(("tt","tmdb:")) and m["type"]=="series" and m["poster"].startswith("https://") for m in meta)
    assert all(m["id"] not in total_ids for m in meta),(slug,"cross-category duplicate")
    total_ids.update(m["id"] for m in meta)
    print("VALID",slug,expected,"OFFSETS",requested,"EMPTY_TERMINAL",skip)
assert len(total_ids)==sum(status["counts"].values())
assert all(count >= 25 for count in status["counts"].values()),"Unexpected large reduction in a catalogue"
assert status["source"]=="TMDB"
print("TOTAL",len(total_ids),"REVIEW_QUEUE",status["reviewCandidates"])
