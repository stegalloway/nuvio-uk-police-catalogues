#!/usr/bin/env python3
"""Refresh two public, UK-policing Stremio catalogues using only TMDB metadata."""
import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
EDITORIAL = ROOT / "config" / "editorial.json"
DISCOVERED = ROOT / "data" / "discovered.json"
REVIEW = ROOT / "data" / "review-queue.json"
DOCS = ROOT / "docs"
PAGE_SIZE = 20
BASE_URL = "https://api.themoviedb.org/3"
TODAY = dt.datetime.now(dt.timezone.utc).date()
UK_BROADCASTERS = {99, 4, 332, 26, 9, 214, 1892, 2461, 497, 259, 578}
UK_NETWORK_HINTS = ("bbc", "channel 4", "channel 5", "itv", "sky", "really", "5action", "5star", "5spike", "5 (", "uktv")
OFFSHORE_MARKERS = ("australian police", "australia police", "new south wales", "nypd", "lapd", "american police", "u.s. police", "indian police", "canadian police", "los angeles police")
STRONG_TITLE = re.compile(r"\b(police|cops?|coppers|detectives?|interceptors?|constables?|custody|bodycam|body\s+cam|armed\s+response|crime\s+squad|crime\s+unit|road\s+wars|patrol)\b", re.I)
INVESTIGATIONS = re.compile(r"detectiv|major crime|murder|cold case|forensic|suspect|corruption|undercover|investigat|manhunt|sex trafficking|hunting|case", re.I)
POLICE_CONTEXT = re.compile(r"\b(police|constabl|officers?|detectives?|policing|traffic\s+cop|metropolitan|criminal\s+investigation|crime\s+squad|police\s+force)\b", re.I)
NEGATIVE = re.compile(r"\b(fictional|scripted drama|crime drama|police procedural drama|mockumentary|satirical)\b", re.I)

def load_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default

def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")

def write_catalog_pages(folder, slug, metas):
    """Serve count-based pagination, including offsets cached before a refresh."""
    save_json(folder / (slug + ".json"), {"metas": metas[:PAGE_SIZE]})
    pages = folder / slug
    total = len(metas)
    offsets = set(range(0, max(60, total + 2 * PAGE_SIZE), PAGE_SIZE))
    # Nuvio advances by the raw number returned: 20 + 8 requests skip=28.
    offsets.add(total)
    for path in pages.glob("skip=*.json"):
        match = re.fullmatch(r"skip=(\d+)\.json", path.name)
        if match:
            offsets.add(int(match.group(1)))
    # A cached old offset may now fall inside a larger catalogue. Keep its
    # subsequent pages reachable, and empty old routes after a smaller refresh.
    for skip in tuple(offsets):
        offsets.update(range(skip, total, PAGE_SIZE))
    for skip in sorted(offsets):
        save_json(pages / f"skip={skip}.json", {"metas": metas[skip:skip + PAGE_SIZE]})

class TMDB:
    def __init__(self, token):
        if not token:
            raise RuntimeError("TMDB_ACCESS_TOKEN GitHub Actions secret is missing")
        self.token = token
        self.cache = {}
    def get(self, path, query=None):
        url = BASE_URL + path
        if query: url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"Authorization":"Bearer " + self.token, "User-Agent":"Nuvio-UK-Police-Catalogues/1.0", "Accept":"application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.load(r)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3: raise
            except (TimeoutError, urllib.error.URLError):
                if attempt == 3:raise
            time.sleep(2 ** attempt)
    def detail(self, tmdb_id):
        if tmdb_id not in self.cache:
            self.cache[tmdb_id] = self.get(f"/tv/{tmdb_id}", {"append_to_response":"external_ids,keywords"})
        return self.cache[tmdb_id]

def date_or_none(raw):
    try: return dt.date.fromisoformat(raw[:10]) if raw else None
    except ValueError: return None

def years_ago(years):
    try: return TODAY.replace(year=TODAY.year-years)
    except ValueError: return TODAY.replace(year=TODAY.year-years, day=28)

def months_ago(months):
    y,m=divmod(TODAY.year*12 + TODAY.month - 1 - months,12)
    return dt.date(y,m+1,min(TODAY.day,28))

def air_date(tv):
    episode=(tv.get("last_episode_to_air") or {})
    candidates=[date_or_none(episode.get("air_date")), date_or_none(tv.get("last_air_date"))]
    return max((x for x in candidates if x and x <= TODAY),default=None)

def eligible(tv, manual=False):
    if manual:return True
    first=date_or_none(tv.get("first_air_date"))
    latest=air_date(tv)
    return bool((first and years_ago(5)<=first<=TODAY) or (latest and latest>=months_ago(24)))

def category_from_text(tv):
    title=tv.get("name") or ""
    desc=tv.get("overview") or ""
    return "police-investigations" if INVESTIGATIONS.search(title) or (INVESTIGATIONS.search(desc) and not STRONG_TITLE.search(title)) else "on-duty"

def confidence(tv):
    title=(tv.get("name") or "").lower()
    desc=(tv.get("overview") or "").lower()
    full=title+" "+desc
    first=date_or_none(tv.get("first_air_date"))
    network=tv.get("networks") or []
    network_match=any(x.get("id") in UK_BROADCASTERS or any(label in (x.get("name") or "").lower() for label in UK_NETWORK_HINTS) for x in network)
    doc=any(x.get("id") in (99,10764) for x in (tv.get("genres") or []))
    if ("GB" not in tv.get("origin_country",[]) or not doc or not network_match or
        not first or first>TODAY or NEGATIVE.search(full) or any(x in full for x in OFFSHORE_MARKERS)):
        return None
    specific=bool(STRONG_TITLE.search(title))
    context=bool(POLICE_CONTEXT.search(desc))
    # A title with no policing context is only a review candidate.
    if not specific and not context:return None
    score=2 + 4*int(specific) + 2*int(context)
    if any(x in full for x in ("britain","british","london","england","scotland","wales","west midlands","merseyside","geordie","manchester","county police")):
        score+=1
    return {"score":score,"category":category_from_text(tv),
            "autoEligible":specific and context and score>=8 and first>=months_ago(6)}

def review_details(tv):
    """What a person needs to judge a review candidate without opening TMDB."""
    overview=" ".join((tv.get("overview") or "").split())
    if len(overview)>240:overview=overview[:240].rsplit(" ",1)[0]+"…"
    return {"network":", ".join(x.get("name") or "" for x in tv.get("networks") or []),
            "episodes":tv.get("number_of_episodes"),"seasons":tv.get("number_of_seasons"),
            "overview":overview}

def title_key(name):
    return re.sub(r"[^a-z0-9]","",(name or "").casefold())

def to_meta(tv):
    number=int(tv["id"])
    x=(tv.get("external_ids") or {}).get("imdb_id") or ""
    stream_id=x if re.fullmatch(r"tt\d+",x) else f"tmdb:{number}"
    artwork=tv.get("poster_path")
    if not artwork and x:
        poster=f"https://images.metahub.space/poster/medium/{x}/img"
    elif artwork:
        poster="https://image.tmdb.org/t/p/w500/"+artwork.lstrip("/")
    else:
        raise ValueError(f"No poster for TV {number}; candidate must remain in review queue")
    backdrop=tv.get("backdrop_path")
    return {
        "id":stream_id,"type":"series","name":tv.get("name") or str(number),
        "poster":poster,"posterShape":"poster",
        "description":tv.get("overview") or "","releaseInfo":(tv.get("first_air_date") or "")[:4],
        "genres":[x["name"] for x in tv.get("genres",[])],
        "background":("https://image.tmdb.org/t/p/w1280/"+backdrop.lstrip("/")) if backdrop else poster
    }

def discover(tmdb, editorial, previous, seeded, already):
    """Scan recent and historic UK TV documentary premieres. Double-confirm auto additions."""
    past=years_ago(editorial["firstAiredWithinYears"])
    query={"with_origin_country":"GB","with_genres":"99|10764",
           "first_air_date.gte":past.isoformat(),"first_air_date.lte":TODAY.isoformat(),
           "sort_by":"first_air_date.desc","include_adult":"false"}
    page1=tmdb.get("/discover/tv",{**query,"page":1})
    count=min(int(page1.get("total_pages",0)),150)
    pages=[page1]
    if count>1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            pages.extend(pool.map(lambda n:tmdb.get("/discover/tv",{**query,"page":n}),range(2,count+1)))
    matches={}
    for page in pages:
        for item in page.get("results",[]):
            name=item.get("name") or ""
            overview=item.get("overview") or ""
            potential=(STRONG_TITLE.search(name) or
                (POLICE_CONTEXT.search(overview) and re.search(r"detectiv|crime|murder|999|police|interceptor|custody|forensic|manhunt",name,re.I)))
            if potential and isinstance(item.get("id"),int):
                matches[item["id"]]=item
    previous_by_id={int(x["tmdbId"]):x for x in previous.get("candidates",[]) if isinstance(x.get("tmdbId"),int)}
    for tmdb_id,meta in list(previous_by_id.items()):
        if meta.get("status")=="pending" and tmdb_id not in matches and meta.get("lastSeen"):
            # Keep recently considered shows even if upstream pagination changes.
            if date_or_none(meta.get("lastSeen")) and date_or_none(meta["lastSeen"])>=months_ago(1):
                matches[tmdb_id]=meta
    blocked=set(editorial.get("excludedTmdbIds",[]))
    name_seen={title_key(x.get("name")) for x in seeded.values()}
    for id in already:
        if id in tmdb.cache:name_seen.add(title_key(tmdb.cache[id].get("name")))
    review=[]
    new={"on-duty":[],"police-investigations":[]}
    error_count=0
    def check(tid):
        try:
            return tid,tmdb.detail(tid),None
        except Exception as exc:
            return tid,None,str(exc)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        fetched=list(pool.map(check,(x for x in matches if x not in blocked and x not in already)))
    for id,show,error in fetched:
        if error:
            error_count+=1
            continue
        signals=confidence(show)
        if not signals or not eligible(show): continue
        normalized=title_key(show.get("name"))
        if normalized in name_seen: continue
        old=previous_by_id.get(id,{})
        first_seen=old.get("firstSeen") or TODAY.isoformat()
        observed=max(0,int(old.get("observations",0)))+1
        double_checked=(date_or_none(first_seen) or TODAY)<TODAY and observed>=2
        poster_ok=bool(show.get("poster_path") or (show.get("external_ids") or {}).get("imdb_id"))
        auto=signals["autoEligible"] and double_checked and poster_ok
        if auto:
            new[signals["category"]].append(id)
            name_seen.add(normalized)
            continue
        review.append({"tmdbId":id,"name":show.get("name"),"category":signals["category"],
                       "score":signals["score"],"firstAir":show.get("first_air_date"),
                       "lastAir":(air_date(show) or TODAY).isoformat(),"firstSeen":first_seen,
                       "lastSeen":TODAY.isoformat(),"observations":observed,
                       "status":"pending","eligibleForAuto":signals["autoEligible"],
                       "needsArtwork":not poster_ok,**review_details(show)})
    if error_count:raise RuntimeError(f"TMDB candidate lookups failed for {error_count} titles; keeping last published catalogues intact")
    review.sort(key=lambda x:(x["score"],x["firstAir"] or "",x["name"] or ""),reverse=True)
    return new,{"schema":1,"candidates":review[:250]},len(pages),len(matches)

def render_outputs(tmdb, editorial, discovered, fresh, review, scan_pages, scan_candidates, write=True):
    current={}
    manual={int(x) for x in editorial.get("manualExceptions",{})}
    for slug,category in editorial["categories"].items():
        ids=list(dict.fromkeys(category["tmdbIds"]+discovered.get(slug,[])+fresh.get(slug,[])))
        entries=[]
        for i in ids:
            obj=tmdb.detail(i)
            if not eligible(obj,manual=i in manual):continue
            entries.append((i,obj))
        def order(pair):
            i,x=pair
            return (int(i not in manual),(air_date(x) or date_or_none(x.get("first_air_date")) or dt.date.min).isoformat(),
                    (x.get("first_air_date") or ""), x.get("name") or "")
        entries.sort(key=order,reverse=True)
        unique_names=set()
        unique_ids=set()
        metas=[]
        final_ids=[]
        for id,detail in entries:
            try: meta=to_meta(detail)
            except ValueError:
                if id in category["tmdbIds"]:raise
                continue
            canonical=(title_key(detail.get("name")),meta["releaseInfo"])
            if canonical in unique_names or meta["id"] in unique_ids:continue
            unique_names.add(canonical)
            unique_ids.add(meta["id"])
            final_ids.append(id)
            metas.append(meta)
        current[slug]=(final_ids,metas)
    original=[(slug,len(ids)) for slug,(ids,_) in current.items()]
    if any(len(ids)<25 for ids,_ in current.values()):
        raise RuntimeError("Unexpected catalogue shrinkage: "+str(original)+". Refusing to publish.")
    if not write:
        print("DRY_RUN_COUNTS",original,"NEW",fresh,"REVIEW",len(review["candidates"]))
        return
    docs=DOCS
    manifest={"id":"uk.curated.policedocumentaries","name":"UK Police Documentaries",
              "description":"Hand-curated UK police factual series, refreshed daily using TMDB.",
              "version":"1.0.0","types":["series"],"resources":["catalog"],
              "catalogs":[{"type":"series","id":slug,"name":c["name"],"extra":[{"name":"skip","isRequired":False}]}
                          for slug,c in editorial["categories"].items()]}
    save_json(docs/"manifest.json",manifest)
    for slug,(ids,metas) in current.items():
        write_catalog_pages(docs/"catalog"/"series", slug, metas)
    save_json(DISCOVERED,{"schema":1,
             **{slug:list(dict.fromkeys(discovered.get(slug,[])+fresh.get(slug,[]))) for slug in editorial["categories"]}})
    save_json(REVIEW,review)
    save_json(docs/"status.json",{
        "updatedAtUtc":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "counts":{slug:len(entries[1]) for slug,entries in current.items()},
        "approvedSeedCount":sum(len(x["tmdbIds"]) for x in editorial["categories"].values()),
        "newlyAutoAdded":fresh,"reviewCandidates":len(review["candidates"]),
        "scanPages":scan_pages,"screenedCandidates":scan_candidates,
        "firstAirCutoff":years_ago(5).isoformat(),"oldSeriesLastAirCutoff":months_ago(24).isoformat(),
        "discoveryPolicy":"New high-confidence UK police documentaries need two daily observations; ambiguous matches require review.",
        "source":"TMDB"})
    print("PUBLISHED_COUNTS",original,"NEW",fresh,"REVIEW",len(review["candidates"]))

def run():
    arg=argparse.ArgumentParser(description="Refresh the UK Police Documentaries addon.")
    arg.add_argument("--bootstrap",action="store_true",help="Publish the approved titles only; no new discovery.")
    arg.add_argument("--dry-run",action="store_true")
    options=arg.parse_args()
    editorial=load_json(EDITORIAL)
    discovered=load_json(DISCOVERED,{"schema":1,"on-duty":[],"police-investigations":[]})
    previous_review=load_json(REVIEW,{"schema":1,"candidates":[]})
    tmdb=TMDB(os.environ.get("TMDB_ACCESS_TOKEN"))
    ids=list(dict.fromkeys([i for c in editorial["categories"].values() for i in c["tmdbIds"]]+
                               [i for slug in editorial["categories"] for i in discovered.get(slug,[])]))
    def get_detail(i):return tmdb.detail(i)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for show in pool.map(get_detail,ids):pass
    original_ids={i:tmdb.cache[i] for i in ids}
    fresh={"on-duty":[],"police-investigations":[]}
    review=previous_review
    scan_pages=scan_candidates=0
    if not options.bootstrap:
        fresh,review,scan_pages,scan_candidates=discover(tmdb,editorial,previous_review,original_ids,set(ids))
    render_outputs(tmdb,editorial,discovered,fresh,review,scan_pages,scan_candidates,write=not options.dry_run)

if __name__=="__main__":
    run()
