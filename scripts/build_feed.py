#!/usr/bin/env python3
"""Build today's maker feed -> docs/feed.json (+ dated archive).

Stdlib only. Works with zero keys (channel RSS feeds). Optional:
  YOUTUBE_API_KEY    -> discovery search for maker videos beyond your channel list
  ANTHROPIC_API_KEY  -> a Claude "vibe check" that drops anything doomer-ish the keywords missed
"""
import json, math, os, re, sys, time, datetime as dt
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
CACHE = os.path.join(ROOT, "data", "channel_ids.json")
SUBS = os.path.join(ROOT, "data", "subs.json")
UA = {"User-Agent": "Mozilla/5.0 (maker-feed; +https://github.com)",
      "Accept-Language": "en-US,en;q=0.9"}

MAX_VIDEOS = 24
PER_CHANNEL = 2
WINDOW_HOURS = 48          # "of the day" with some slack for timezones/slow uploaders
FALLBACK_HOURS = 24 * 7    # widen if the day was quiet
MIN_VIDEOS = 12
INDIE_SHARE = 0.5         # at least this fraction of the feed comes from indie channels
INDIE_MAX_SUBS = 100_000  # under this many subscribers = indie
SUBS_MAX_AGE_DAYS = 7     # re-check subscriber counts weekly

# Anything matching these is dropped (except 'always' channels).
BLOCK = [
    r"news", r"breaking", r"econom\w*", r"recession", r"inflation", r"layoffs?",
    r"unemploy\w*", r"collapse", r"doom\w*", r"crisis", r"apocalyp\w*", r"war",
    r"election", r"politic\w*", r"trump", r"biden", r"congress", r"tariffs?",
    r"stock market", r"market crash", r"housing market", r"interest rates?",
    r"the fed", r"bubble", r"is (dead|over|finished)", r"the end of",
    r"we'?re (doomed|screwed)", r"terrifying", r"scary truth", r"dark truth",
    r"warning", r"replace (all|your|us)", r"jobs? (are|is) (gone|over)",
    r"can'?t afford", r"scam", r"lawsuit", r"sued", r"drama",
    r"live ?stream", r"\blive\b", r"podcast", r"reacts?",
]
BLOCK_RE = re.compile(r"\b(" + "|".join(BLOCK) + r")\b|#shorts", re.I)

NS = {"a": "http://www.w3.org/2005/Atom",
      "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


def get(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------- channel resolution ----------
def resolve(handle, cache):
    if handle in cache:
        return cache[handle]
    try:
        html = get("https://www.youtube.com/" + handle)
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
             or re.search(r'"externalId":"(UC[\w-]{22})"', html)
             or re.search(r'"channelId":"(UC[\w-]{22})"', html))
        if m:
            cache[handle] = m.group(1)
            return m.group(1)
    except Exception as e:
        print(f"  ! couldn't resolve {handle}: {e}", file=sys.stderr)
    return None


# ---------- subscriber counts (decide who's indie) ----------
def _num(s):
    s = s.replace(",", "")
    mult = 1_000_000 if s.endswith("M") else 1_000 if s.endswith("K") else 1
    return int(float(s.rstrip("KM")) * mult)


def fetch_subs(cid):
    try:
        html = get(f"https://www.youtube.com/channel/{cid}")
        m = (re.search(r'"content":"([\d.,]+[KM]?) subscribers"', html)
             or re.search(r'"([\d.,]+[KM]?) subscribers"', html))
        return _num(m.group(1)) if m else None
    except Exception as e:
        print(f"  ! subs failed {cid}: {e}", file=sys.stderr)
        return None


def refresh_subs(cids, today):
    cache = json.load(open(SUBS)) if os.path.exists(SUBS) else {}
    stale = [c for c in cids if c not in cache or
             (today - dt.date.fromisoformat(cache[c]["checked"])).days >= SUBS_MAX_AGE_DAYS]
    if stale:
        print(f"Checking subscriber counts for {len(stale)} channels…")
        with ThreadPoolExecutor(8) as ex:
            for c, n in zip(stale, ex.map(fetch_subs, stale)):
                if n is not None:          # keep the old number if YouTube didn't answer
                    cache[c] = {"subs": n, "checked": today.isoformat()}
        json.dump(cache, open(SUBS, "w"), indent=0, sort_keys=True)
    return {c: v["subs"] for c, v in cache.items()}


# ---------- RSS ----------
def parse_rss(xml_text, source):
    out = []
    root = ET.fromstring(xml_text)
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        group = e.find("media:group", NS)
        stats = group.find("media:community/media:statistics", NS) if group is not None else None
        out.append({
            "id": vid,
            "title": e.findtext("a:title", namespaces=NS) or "",
            "channel": e.findtext("a:author/a:name", namespaces=NS) or "",
            "channelId": e.findtext("yt:channelId", namespaces=NS),
            "published": e.findtext("a:published", namespaces=NS),
            "description": (group.findtext("media:description", namespaces=NS) if group is not None else "") or "",
            "views": int(stats.get("views", 0)) if stats is not None else 0,
            "source": source,
        })
    return out


def fetch_channel(cid, source):
    try:
        return parse_rss(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"), source)
    except Exception as e:
        print(f"  ! feed failed {cid}: {e}", file=sys.stderr)
        return []


# ---------- Shorts detection (no key needed) ----------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

_opener = urllib.request.build_opener(_NoRedirect)

def is_short(vid):
    try:
        r = _opener.open(urllib.request.Request(f"https://www.youtube.com/shorts/{vid}", headers=UA, method="HEAD"), timeout=10)
        return r.status == 200
    except urllib.error.HTTPError:
        return False  # 3xx -> regular video
    except Exception:
        return False


# ---------- optional: YouTube Data API discovery ----------
def discover(queries, since, key):
    found = []
    for q in queries:
        try:
            p = urllib.parse.urlencode({"part": "snippet", "q": q, "type": "video", "order": "viewCount",
                                        "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "videoDuration": "medium", "relevanceLanguage": "en",
                                        "maxResults": 8, "key": key})
            for it in json.loads(get("https://www.googleapis.com/youtube/v3/search?" + p)).get("items", []):
                s = it["snippet"]
                found.append({"id": it["id"]["videoId"], "title": s["title"], "channel": s["channelTitle"],
                              "channelId": s["channelId"], "published": s["publishedAt"],
                              "description": s.get("description", ""), "views": 0, "source": "discover"})
        except Exception as e:
            print(f"  ! search failed '{q}': {e}", file=sys.stderr)
    # fill in view counts
    ids = list({v["id"] for v in found})
    for i in range(0, len(ids), 50):
        try:
            p = urllib.parse.urlencode({"part": "statistics", "id": ",".join(ids[i:i+50]), "key": key})
            views = {it["id"]: int(it["statistics"].get("viewCount", 0))
                     for it in json.loads(get("https://www.googleapis.com/youtube/v3/videos?" + p))["items"]}
            for v in found:
                v["views"] = views.get(v["id"], v["views"])
        except Exception as e:
            print(f"  ! stats failed: {e}", file=sys.stderr)
    return found


# ---------- optional: Claude vibe check ----------
def vibe_check(videos, key):
    if not videos:
        return videos
    listing = "\n".join(f'{v["id"]} | {v["channel"]} | {v["title"]} | {v["description"][:160].replace(chr(10), " ")}'
                        for v in videos)
    prompt = ("You curate a YouTube homepage for a hardware engineer and maker. Keep videos about building, "
              "making, engineering, science, creative tech and things that make someone feel hopeful or excited "
              "about the future. Drop doom, fear-mongering, economy/markets, politics, news, outrage, drama, "
              "clickbait gloom and low-effort reaction content. Videos from Fireship or AI Explained are always kept.\n\n"
              "Reply with ONLY a JSON object mapping each kept video id to a short upbeat one-line blurb (max 12 words).\n\n"
              + listing)
    body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 2000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        resp = json.loads(get("https://api.anthropic.com/v1/messages", data=body, timeout=90, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}))
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        keep = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print(f"  ! vibe check skipped: {e}", file=sys.stderr)
        return videos
    out = []
    for v in videos:
        if v["id"] in keep or v.get("always"):
            v["blurb"] = keep.get(v["id"], "")
            out.append(v)
    print(f"  vibe check kept {len(out)}/{len(videos)}")
    return out


# ---------- pipeline ----------
def hours_old(v, now):
    t = dt.datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
    return max((now - t).total_seconds() / 3600, 1.0)


def vph(v, now):
    # views per hour, softened so a 2-hour-old video doesn't get an unfair boost
    return v["views"] / (max(hours_old(v, now), 3) ** 0.85)


def add_baselines(pool, now):
    """Each channel's median views/hour across its recent uploads. Scoring against a
    channel's own baseline means a 5K-sub maker's breakout video can beat a big
    channel's average upload."""
    by = {}
    for v in pool:
        by.setdefault(v["channelId"], []).append(vph(v, now))
    med = {c: sorted(xs)[len(xs) // 2] for c, xs in by.items()}
    for v in pool:
        v["_base"] = med.get(v["channelId"], 0)


def score(v, now):
    base = v.get("_base") or 0
    rel = vph(v, now) / max(base, 1.0) if base else 1.0
    return rel * math.log10(v["views"] + 10)


def select(pool, now, window_h):
    fresh = [v for v in pool if hours_old(v, now) <= window_h]
    fresh.sort(key=lambda v: score(v, now), reverse=True)
    picked, per = [], {}

    def take(cands, limit):
        for v in cands:
            if len(picked) >= limit:
                return
            if v in picked or per.get(v["channelId"], 0) >= PER_CHANNEL:
                continue
            per[v["channelId"]] = per.get(v["channelId"], 0) + 1
            picked.append(v)

    take([v for v in fresh if v.get("tier") == "indie"], math.ceil(MAX_VIDEOS * INDIE_SHARE))
    take(fresh, MAX_VIDEOS)
    picked.sort(key=lambda v: score(v, now), reverse=True)
    return picked


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cfg = json.load(open(os.path.join(ROOT, "channels.json")))
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

    print("Resolving channels…")
    # "indie"/"makers" are accepted for older configs; tiers are now decided by subscriber count
    entries = [(e, "always" if key == "always" else "channel")
               for key in ("always", "channels", "indie", "makers") for e in cfg.get(key, [])]

    def to_id(e):
        if isinstance(e, list):
            return e[1]
        if e.startswith("UC") and len(e) == 24:
            return e
        return resolve(e, cache)

    with ThreadPoolExecutor(8) as ex:
        ids = list(ex.map(lambda et: (to_id(et[0]), et[1]), entries))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
    tier_of = {}
    for cid, tier in ids:
        if cid:
            tier_of.setdefault(cid, tier)
    subs = refresh_subs(list(tier_of), now.date())
    for c, t in tier_of.items():
        if t == "channel":
            tier_of[c] = "indie" if subs.get(c, INDIE_MAX_SUBS) < INDIE_MAX_SUBS else "maker"
    print(f"  {sum(t == 'indie' for t in tier_of.values())} indie channels (< {INDIE_MAX_SUBS:,} subs)")
    always_ids = {c for c, t in tier_of.items() if t == "always"}
    ids = list(tier_of.items())

    print("Fetching feeds…")
    with ThreadPoolExecutor(8) as ex:
        pool = [v for vs in ex.map(lambda p: fetch_channel(*p), [p for p in ids if p[0]]) for v in vs]

    if os.environ.get("YOUTUBE_API_KEY"):
        print("Discovering…")
        pool += discover(cfg.get("search_queries", []), now - dt.timedelta(hours=WINDOW_HOURS),
                         os.environ["YOUTUBE_API_KEY"])

    add_baselines(pool, now)

    # dedupe, flag, keyword-filter
    seen, clean = set(), []
    for v in pool:
        if not v["id"] or v["id"] in seen:
            continue
        seen.add(v["id"])
        v["always"] = v["channelId"] in always_ids
        v["tier"] = tier_of.get(v["channelId"], "discover")
        v["subs"] = subs.get(v["channelId"])
        if not v["always"] and BLOCK_RE.search(v["title"]):
            continue
        clean.append(v)

    # pick a candidate set, drop Shorts, then vibe-check
    candidates = [v for v in clean if hours_old(v, now) <= FALLBACK_HOURS]
    candidates.sort(key=lambda v: score(v, now), reverse=True)
    candidates = candidates[: MAX_VIDEOS * 3]
    with ThreadPoolExecutor(8) as ex:
        shorts = dict(zip([v["id"] for v in candidates], ex.map(is_short, [v["id"] for v in candidates])))
    candidates = [v for v in candidates if not shorts.get(v["id"])]
    if os.environ.get("ANTHROPIC_API_KEY"):
        candidates = vibe_check(candidates, os.environ["ANTHROPIC_API_KEY"])

    picked = select(candidates, now, WINDOW_HOURS)
    if len(picked) < MIN_VIDEOS:
        picked = select(candidates, now, FALLBACK_HOURS)

    for v in picked:
        v["description"] = v["description"][:280]
        v.pop("_base", None)
    feed = {"generated": now.isoformat(timespec="seconds"), "count": len(picked), "videos": picked}

    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    json.dump(feed, open(os.path.join(DOCS, "feed.json"), "w"), indent=1)
    day = now.strftime("%Y-%m-%d")
    json.dump(feed, open(os.path.join(DOCS, "archive", f"{day}.json"), "w"))
    idx_path = os.path.join(DOCS, "archive", "index.json")
    days = json.load(open(idx_path)) if os.path.exists(idx_path) else []
    if day not in days:
        days.append(day)
    json.dump(sorted(days, reverse=True)[:120], open(idx_path, "w"))
    print(f"Done: {len(picked)} videos from {len(pool)} candidates.")


if __name__ == "__main__":
    main()
