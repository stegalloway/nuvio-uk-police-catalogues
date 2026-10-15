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
    meta=[]
    for skip in range(0,expected+20,20):
        meta+=read(folder/slug/(f"skip={skip}.json"))["metas"]
    assert len(meta)==expected,(slug,len(meta),expected)
    assert len({m["id"] for m in meta})==expected
    assert len({m["name"].casefold() for m in meta})==expected
    assert all(m["id"].startswith(("tt","tmdb:")) and m["type"]=="series" and m["poster"].startswith("https://") for m in meta)
    assert all(m["id"] not in total_ids for m in meta),(slug,"cross-category duplicate")
    total_ids.update(m["id"] for m in meta)
    print("VALID",slug,expected,"PAGINATED",len(first),len(meta)-len(first))
assert len(total_ids)==sum(status["counts"].values())
assert all(count >= 25 for count in status["counts"].values()),"Unexpected large reduction in a catalogue"
assert status["source"]=="TMDB"
print("TOTAL",len(total_ids),"REVIEW_QUEUE",status["reviewCandidates"])
