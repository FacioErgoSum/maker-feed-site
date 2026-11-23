#!/usr/bin/env python3
"""Build the maker feed + stats data.

Outputs
  docs/feed.json               today's feed
  docs/archive/YYYY-MM-DD.json one feed per day (missing days in the last 30 are backfilled)
  docs/videos.json             every tracked video from the last 30 days, enriched, for stats.html
  data/videos.json             the working store (view history, enrichment cache)

Works with zero keys (channel RSS + public pages). Optional:
  YOUTUBE_API_KEY    -> discovery search for maker videos beyond your channel list
  ANTHROPIC_API_KEY  -> a Claude "vibe check" that drops anything doomer-ish the keywords missed
Pillow (pip install pillow) enables thumbnail analysis; without it that step is skipped.
"""
import io, json, math, os, re, sys, statistics, datetime as dt
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "channel_ids.json")
SUBS = os.path.join(DATA, "subs.json")
STORE = os.path.join(DATA, "videos.json")
UA = {"User-Agent": "Mozilla/5.0 (maker-feed; +https://github.com)",
      "Accept-Language": "en-US,en;q=0.9"}

MAX_VIDEOS = 24
PER_CHANNEL = 2
WINDOW_HOURS = 48          # "of the day" with some slack for timezones/slow uploaders
FALLBACK_HOURS = 24 * 7    # widen if the day was quiet
MIN_VIDEOS = 12
INDIE_SHARE = 0.5          # at least this fraction of the feed comes from indie channels
INDIE_MAX_SUBS = 100_000   # under this many subscribers = indie
SUBS_MAX_AGE_DAYS = 7      # re-check subscriber counts weekly
HISTORY_DAYS = 30          # days of archive + stats
STORE_DAYS = 40            # keep a little extra in the working store
SHORT_MAX_SECONDS = 180    # anything longer can't be a Short

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


def get(url, data=None, headers=None, timeout=20, raw=False):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return body if raw else body.decode("utf-8", "replace")


def load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- channel resolution ----------
def resolve(handle, cache):
    if handle in cache:
        return cache[handle]
    try:
        html = get("https://www.youtube.com/" + handle)
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
             or re.search(r'"externalId":"(UC[\w-]{22})"', html))
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
    """Subscriber count + avatar URL from the channel page."""
    try:
        html = get(f"https://www.youtube.com/channel/{cid}")
    except Exception as e:
        print(f"  ! channel page failed {cid}: {e}", file=sys.stderr)
        return None
    m = (re.search(r'"content":"([\d.,]+[KM]?) subscribers"', html)
         or re.search(r'"([\d.,]+[KM]?) subscribers"', html))
    a = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if not m:
        return None
    return {"subs": _num(m.group(1)), "avatar": re.sub(r"=s\d+", "=s88", a.group(1)) if a else None}


def refresh_subs(cids, today):
    cache = load(SUBS, {})
    stale = [c for c in cids if c not in cache or "avatar" not in cache[c] or
             (today - dt.date.fromisoformat(cache[c]["checked"])).days >= SUBS_MAX_AGE_DAYS]
    if stale:
        print(f"Checking subscriber counts for {len(stale)} channels…")
        with ThreadPoolExecutor(8) as ex:
            for c, info in zip(stale, ex.map(fetch_subs, stale)):
                if info:                   # keep the old numbers if YouTube didn't answer
                    cache[c] = {**info, "checked": today.isoformat()}
        json.dump(cache, open(SUBS, "w"), indent=0, sort_keys=True)
    return cache


# ---------- RSS ----------
def parse_rss(xml_text, source):
    out = []
    root = ET.fromstring(xml_text)
    for e in root.findall("a:entry", NS):
        group = e.find("media:group", NS)
        stats = group.find("media:community/media:statistics", NS) if group is not None else None
        stars = group.find("media:community/media:starRating", NS) if group is not None else None
        out.append({
            "id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS) or "",
            "channel": e.findtext("a:author/a:name", namespaces=NS) or "",
            "channelId": e.findtext("yt:channelId", namespaces=NS),
            "published": e.findtext("a:published", namespaces=NS),
            "description": (group.findtext("media:description", namespaces=NS) if group is not None else "") or "",
            "views": int(stats.get("views", 0)) if stats is not None else 0,
            "likes": int(stars.get("count", 0)) if stars is not None else None,
            "source": source,
        })
    return out


def fetch_channel(cid, source):
    try:
        return parse_rss(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"), source)
    except Exception as e:
        print(f"  ! feed failed {cid}: {e}", file=sys.stderr)
        return []


# ---------- per-video enrichment (cached in the store, done once per video) ----------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

_opener = urllib.request.build_opener(_NoRedirect)


def is_short(vid):
    try:
        r = _opener.open(urllib.request.Request(f"https://www.youtube.com/shorts/{vid}", headers=UA, method="HEAD"), timeout=10)
        return r.status == 200
    except Exception:
        return False  # 3xx -> regular video


def watch_details(vid):
    """Length, category, tags, likes and live flag from the public watch page."""
    try:
        html = get(f"https://www.youtube.com/watch?v={vid}")
    except Exception as e:
        print(f"  ! watch page failed {vid}: {e}", file=sys.stderr)
        return {}
    out = {}
    if m := re.search(r'"lengthSeconds":"(\d+)"', html):
        out["duration"] = int(m.group(1))
    if m := re.search(r'"category":"([^"]+)"', html):
        out["category"] = m.group(1).replace("\\u0026", "&")
    if m := re.search(r'"keywords":\[([^\]]*)\]', html):
        try:
            out["tags"] = json.loads("[" + m.group(1) + "]")[:12]
        except ValueError:
            pass
    if m := re.search(r'"likeCount":"?(\d+)', html):
        out["likes"] = int(m.group(1))
    out["live"] = '"isLiveContent":true' in html
    if "duration" in out:
        out["short"] = out["duration"] <= SHORT_MAX_SECONDS and is_short(vid)
    return out


try:
    from PIL import Image, ImageFilter, ImageStat
except ImportError:
    Image = None


def thumb_stats(vid):
    """Simple, explainable thumbnail measurements (all 0-1)."""
    if Image is None:
        return None
    try:
        img = Image.open(io.BytesIO(get(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", raw=True))).convert("RGB")
    except Exception as e:
        print(f"  ! thumb failed {vid}: {e}", file=sys.stderr)
        return None
    img = img.resize((160, 90))
    gray = img.convert("L")
    hsv = img.convert("HSV")
    h, s, v = hsv.split()
    px = list(img.getdata())
    rg = [r - g for r, g, b in px]
    yb = [0.5 * (r + g) - b for r, g, b in px]
    colorful = (math.sqrt(statistics.pvariance(rg) + statistics.pvariance(yb))
                + 0.3 * math.sqrt(statistics.mean(rg) ** 2 + statistics.mean(yb) ** 2))
    sat_px = list(s.getdata())
    hue_px = list(h.getdata())
    warm = sum(1 for hh, ss in zip(hue_px, sat_px) if ss > 77 and (hh < 32 or hh > 235)) / len(px)
    edges = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    pal = img.quantize(colors=4, method=Image.Quantize.MEDIANCUT)
    counts = sorted(pal.getcolors(), reverse=True)
    rgb = pal.getpalette()
    swatches = [("#%02x%02x%02x" % tuple(rgb[i * 3:i * 3 + 3]), round(c / len(px), 2)) for c, i in counts[:3]]
    return {
        "brightness": round(ImageStat.Stat(gray).mean[0] / 255, 3),
        "contrast": round(ImageStat.Stat(gray).stddev[0] / 128, 3),
        "saturation": round(statistics.mean(sat_px) / 255, 3),
        "colorfulness": round(min(colorful / 150, 1), 3),
        "warmth": round(warm, 3),
        "busyness": round(min(edges / 64, 1), 3),
        "palette": swatches,
    }


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


# ---------- optional: Claude vibe check (once per video, cached) ----------
def vibe_check(videos, key):
    """Returns {id: blurb} for kept videos, or None if the call failed."""
    listing = "\n".join(f'{v["id"]} | {v["channel"]} | {v["title"]} | {v["description"][:160].replace(chr(10), " ")}'
                        for v in videos)
    prompt = ("You curate a YouTube homepage for a hardware engineer and maker. Keep videos about building, "
              "making, engineering, science, creative tech and things that make someone feel hopeful or excited "
              "about the future. Drop doom, fear-mongering, economy/markets, politics, news, outrage, drama, "
              "clickbait gloom and low-effort reaction content. Videos from Fireship or AI Explained are always kept.\n\n"
              "Reply with ONLY a JSON object mapping each kept video id to a short upbeat one-line blurb (max 12 words).\n\n"
              + listing)
    body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 4000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        resp = json.loads(get("https://api.anthropic.com/v1/messages", data=body, timeout=120, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}))
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print(f"  ! vibe check skipped: {e}", file=sys.stderr)
        return None


# ---------- scoring ----------
def hours_old(v, now):
    return max((now - parse_time(v["published"])).total_seconds() / 3600, 1.0)


def vph(v, now):
    # views per hour, softened so a 2-hour-old video doesn't get an unfair boost
    return v["views"] / (max(hours_old(v, now), 3) ** 0.85)


def channel_baselines(pool, now):
    """Median views/hour per channel across its recent uploads. Scoring against a
    channel's own baseline means a 5K-sub maker's breakout video can beat a big
    channel's average upload."""
    by = {}
    for v in pool:
        by.setdefault(v["channelId"], []).append(vph(v, now))
    return {c: sorted(xs)[len(xs) // 2] for c, xs in by.items()}


def perf(v, now, base):
    b = base.get(v["channelId"]) or 0
    return vph(v, now) / max(b, 1.0) if b else 1.0


def score(v, now, base):
    return perf(v, now, base) * math.log10(v["views"] + 10)


def select(pool, now, window_h, base):
    fresh = [v for v in pool if 0 <= (now - parse_time(v["published"])).total_seconds() / 3600 <= window_h]
    fresh.sort(key=lambda v: score(v, now, base), reverse=True)
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
    picked.sort(key=lambda v: score(v, now, base), reverse=True)
    return picked


def build_day(pool, now, base):
    picked = select(pool, now, WINDOW_HOURS, base)
    if len(picked) < MIN_VIDEOS:
        picked = select(pool, now, FALLBACK_HOURS, base)
    keys = ("id", "title", "channel", "channelId", "published", "description", "views",
            "source", "always", "tier", "subs", "avatar", "blurb", "duration")
    vids = [{k: v.get(k) for k in keys} for v in picked]
    for v in vids:
        v["description"] = (v["description"] or "")[:280]
    return {"generated": now.isoformat(timespec="seconds"), "count": len(vids), "videos": vids}


# ---------- pipeline ----------
def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    cfg = json.load(open(os.path.join(ROOT, "channels.json")))
    cache = load(CACHE, {})
    os.makedirs(DATA, exist_ok=True)

    print("Resolving channels…")
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
    json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
    tier_of = {}
    for cid, tier in ids:
        if cid:
            tier_of.setdefault(cid, tier)
    chan_info = refresh_subs(list(tier_of), today)
    subs = {c: v["subs"] for c, v in chan_info.items()}
    avatars = {c: v.get("avatar") for c, v in chan_info.items()}
    for c, t in tier_of.items():
        if t == "channel":
            tier_of[c] = "indie" if subs.get(c, INDIE_MAX_SUBS) < INDIE_MAX_SUBS else "maker"
    print(f"  {sum(t == 'indie' for t in tier_of.values())} indie channels (< {INDIE_MAX_SUBS:,} subs)")

    print("Fetching feeds…")
    with ThreadPoolExecutor(8) as ex:
        pool = [v for vs in ex.map(lambda p: fetch_channel(*p), list(tier_of.items())) for v in vs]
    if os.environ.get("YOUTUBE_API_KEY"):
        print("Discovering…")
        pool += discover(cfg.get("search_queries", []), now - dt.timedelta(hours=WINDOW_HOURS),
                         os.environ["YOUTUBE_API_KEY"])
    base = channel_baselines(pool, now)

    # ---- update the store ----
    store = load(STORE, {})
    cutoff = now - dt.timedelta(days=STORE_DAYS)
    for v in pool:
        if not v["id"] or parse_time(v["published"]) < cutoff:
            continue
        s = store.setdefault(v["id"], {"first_seen": today.isoformat()})
        for k in ("title", "channel", "channelId", "published", "description", "views", "source"):
            s[k] = v[k]
        s["description"] = s["description"][:400]
        if v.get("likes") is not None:
            s["likes"] = v["likes"]
        s["tier"] = tier_of.get(v["channelId"], "discover")
        s["always"] = s["tier"] == "always"
        s["subs"] = subs.get(v["channelId"])
        s["avatar"] = avatars.get(v["channelId"])
        s["perf"] = round(perf(v, now, base), 3)
        s["blocked"] = (not s["always"]) and bool(BLOCK_RE.search(v["title"]))
        hist = s.setdefault("history", [])
        if not hist or hist[-1][0] != today.isoformat():
            hist.append([today.isoformat(), v["views"]])
    for vid in [k for k, s in store.items() if parse_time(s["published"]) < cutoff]:
        del store[vid]

    todo = [vid for vid, s in store.items() if "duration" not in s]
    if todo:
        print(f"Enriching {len(todo)} new videos (length, tags, Shorts)…")
        with ThreadPoolExecutor(8) as ex:
            for vid, d in zip(todo, ex.map(watch_details, todo)):
                store[vid].update(d)
    if Image is not None:
        todo = [vid for vid, s in store.items() if "thumb" not in s and not s.get("short")]
        if todo:
            print(f"Analyzing {len(todo)} thumbnails…")
            with ThreadPoolExecutor(8) as ex:
                for vid, t in zip(todo, ex.map(thumb_stats, todo)):
                    if t:
                        store[vid]["thumb"] = t
    else:
        print("  (Pillow not installed: skipping thumbnail analysis)")

    if os.environ.get("ANTHROPIC_API_KEY"):
        todo = [s | {"id": vid} for vid, s in store.items()
                if "vibe" not in s and not s.get("short") and not s.get("blocked")]
        for i in range(0, len(todo), 80):
            batch = todo[i:i + 80]
            keep = vibe_check(batch, os.environ["ANTHROPIC_API_KEY"])
            if keep is None:
                break
            for v in batch:
                store[v["id"]]["vibe"] = v["id"] in keep or v.get("always", False)
                if keep.get(v["id"]):
                    store[v["id"]]["blurb"] = keep[v["id"]]
        print("  vibe check done")
    json.dump(store, open(STORE, "w"), separators=(",", ":"))

    # ---- feed candidates: tracked, not blocked, not a Short, not live, passed the vibe check ----
    cands = [s | {"id": vid} for vid, s in store.items()
             if not s.get("blocked") and not s.get("short") and not s.get("live") and s.get("vibe", True)]

    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    feed = build_day(cands, now, base)
    json.dump(feed, open(os.path.join(DOCS, "feed.json"), "w"), indent=1)
    json.dump(feed, open(os.path.join(DOCS, "archive", f"{today}.json"), "w"))

    # ---- backfill any missing days in the last month ----
    # Past days are rebuilt from what each channel posted that day, ranked with today's view counts.
    filled = 0
    for back in range(1, HISTORY_DAYS):
        day = today - dt.timedelta(days=back)
        path = os.path.join(DOCS, "archive", f"{day}.json")
        if os.path.exists(path):
            continue
        end = dt.datetime.combine(day, dt.time(23, 59), dt.timezone.utc)
        past = build_day(cands, end, base)
        if past["count"]:
            past["backfilled"] = True
            json.dump(past, open(path, "w"))
            filled += 1
    if filled:
        print(f"  backfilled {filled} past days")

    days = sorted((f[:-5] for f in os.listdir(os.path.join(DOCS, "archive"))
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f)), reverse=True)
    for old in days[HISTORY_DAYS + 30:]:
        os.remove(os.path.join(DOCS, "archive", f"{old}.json"))
    json.dump(days[:HISTORY_DAYS + 30], open(os.path.join(DOCS, "archive", "index.json"), "w"))

    # ---- stats export ----
    month = now - dt.timedelta(days=HISTORY_DAYS)
    keys = ("title", "channel", "channelId", "published", "views", "likes", "tier", "subs", "avatar", "blurb", "perf",
            "duration", "category", "tags", "short", "live", "blocked", "thumb", "history")
    export = [{"id": vid, **{k: s.get(k) for k in keys}}
              for vid, s in store.items() if parse_time(s["published"]) >= month]
    export.sort(key=lambda v: v["published"], reverse=True)
    json.dump({"generated": now.isoformat(timespec="seconds"), "videos": export},
              open(os.path.join(DOCS, "videos.json"), "w"), separators=(",", ":"))
    print(f"Done: {feed['count']} in today's feed, {len(export)} videos tracked this month.")


if __name__ == "__main__":
    main()
