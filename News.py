import bisect
import csv
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CONFIG_FILE = "config.json"
OUTPUT_DIR = "output"
SHOW_MERGES = "--show-merges" in sys.argv

BASE_COLUMNS = [
    "score",
    "base_score",
    "relevant",
    "repeat_type",
    "publisher",
    "title",
    "url",
    "relevancy",
    "source_score",
    "buzz",
    "matched",
    "direction",
    "repeat",
    "repeat_sources",
    "also_reported_by",
    "series_editions",
    "published",
    "published_utc",
    "price_gap_min",
]

ATOM = "{http://www.w3.org/2005/Atom}"
TAG_RE = re.compile(r"<[^>]+>")
SUFFIX = r"(?:s|es|ها|های|ی|ای)?"  # plurals / common Persian suffixes for whole-word mode
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)

# ---------- duplicate-detection defaults (config "dedupe" block can tune/extend) ----------
DEFAULT_DEDUPE = {
    "similarity_threshold": 0.5,   # word-overlap needed to call two headlines the same news
    "min_shared_words": 3,
    "window_hours": 48,            # similar headlines must be published this close together
    "boost_per_source": 0.05,      # score bonus per extra outlet (relevant news only)
    "max_boost": 0.2,
    "stopwords": [
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "by",
        "from", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
        "these", "those", "after", "over", "amid", "says", "said", "say", "new", "news",
        "latest", "update", "live", "report", "reports", "reported", "will", "has", "have",
        "had", "not", "but", "than", "into", "about", "up", "down", "out", "who", "what",
        "why", "how", "s", "t",
        "از", "به", "در", "با", "که", "را", "این", "آن", "و", "برای", "تا", "بر", "هم",
        "می", "شد", "شده", "است", "بود", "خواهد", "یک", "هر", "نیز", "اما", "یا", "پس",
    ],
    # canonical word -> word prefixes that should count as the same thing
    "synonyms": {
        "fall": ["fall", "fell", "drop", "plung", "plummet", "slump", "tumbl", "slid", "sink",
                 "sank", "crash", "declin", "weaken", "slip", "کاهش", "سقوط", "ریزش", "افت"],
        "rise": ["rise", "rising", "rose", "surg", "soar", "jump", "climb", "rall", "spik",
                 "gain", "hike", "leap", "افزایش", "صعود", "جهش", "رشد"],
        "usd": ["dollar", "usd", "greenback", "دلار"],
        "rial": ["rial", "toman", "ریال", "تومان"],
        "iran": ["iran", "tehran", "ایران", "تهران"],
        "gold": ["gold", "bullion", "طلا"],
        "coin": ["coin", "سکه"],
        "crypto": ["crypto", "bitcoin", "btc", "tether", "usdt", "رمزارز", "تتر"],
        "price": ["price", "rate", "قیمت", "نرخ"],
    },
}

# ---------- text normalization ----------
PERSIAN_FIX = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",
                             "\u200c": " ", "\u200f": None, "\u200e": None})
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

MONTHS = ("(?:january|february|march|april|may|june|july|august|september|october|november|"
          "december|sept|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
          "فروردین|اردیبهشت|خرداد|مرداد|شهریور|اسفند|بهمن|آبان|آذر|مهر|تیر|دی|"
          "ژانویه|فوریه|مارس|آوریل|ژوئن|ژوئیه|اوت|سپتامبر|اکتبر|نوامبر|دسامبر)")
DATE_RES = [
    re.compile(rf"(?<!\w)\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTHS}(?!\w)(?:\s+\d{{4}})?"),
    re.compile(rf"(?<!\w){MONTHS}(?!\w)\s+\d{{1,2}}(?!\d)(?:st|nd|rd|th)?(?:\s+\d{{4}})?"),
    re.compile(r"(?<!\w)\d{4}\s+\d{1,2}\s+\d{1,2}(?!\w)"),
    re.compile(r"(?<!\w)\d{1,2}\s+\d{1,2}\s+\d{2,4}(?!\w)"),
]
RELATIVE_RE = re.compile(
    r"(?<!\w)(?:today|tomorrow|yesterday|tonight|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|امروز|فردا|دیروز|سه شنبه|یکشنبه|دوشنبه|چهارشنبه|پنجشنبه|شنبه|جمعه)(?!\w)")


def normalize(text):
    text = (text or "").translate(PERSIAN_FIX).translate(DIGITS).lower()
    text = re.sub(r"(?<=\d)[,٬](?=\d{3})", "", text)  # 1,000,000 -> 1000000
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def make_template(norm):
    """Remove dates so daily editions of the same headline become identical."""
    out, had_date = norm, False
    for rx in DATE_RES + [RELATIVE_RE]:
        out, n = rx.subn(" ", out)
        had_date = had_date or n > 0
    return " ".join(out.split()), had_date


def stem(w):
    if any(ch.isdigit() for ch in w):
        return w
    for suf in ("های", "ها"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w[:5]  # crude stemmer: rise/rises/rising -> same 5-letter prefix


def tokenize(text, stop, syn_pairs):
    out = set()
    for w in text.split():
        if w in stop or (len(w) < 2 and not w.isdigit()):
            continue
        mapped = next((c for prefix, c in syn_pairs if w.startswith(prefix)), None)
        out.add(mapped if mapped else stem(w))
    return out


def newest(members):
    return max((m["dt"] or MIN_DT) for m in members)


def within_window(a, b, hours):
    if a is None or b is None:
        return True
    return abs((a - b).total_seconds()) <= hours * 3600


def dedupe(items, d):
    """Collapse exact copies, daily series and same-news-different-wording.
    Returns (result rows, notes for --show-merges)."""
    if not items:
        return [], []
    notes = []
    stop = {normalize(w) for w in d["stopwords"]}
    syn_pairs = sorted(
        ((normalize(p), canon) for canon, prefixes in d["synonyms"].items() for p in prefixes),
        key=lambda x: -len(x[0]),
    )

    for it in items:
        it["norm"] = normalize(it["title"])
        it["template"], it["had_date"] = make_template(it["norm"])
        it["tokens"] = tokenize(it["template"], stop, syn_pairs)

    # Type 1: exact titles
    groups = {}
    for it in items:
        groups.setdefault(it["norm"] or it["url"], []).append(it)

    # Type 3: daily series (same headline, only the date differs) -> keep newest edition
    by_template = {}
    for key, members in groups.items():
        t = members[0]["template"]
        if len(t.split()) >= 3 and members[0]["had_date"]:
            by_template.setdefault(t, []).append(key)
    stale, editions = set(), {}
    for keys in by_template.values():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda k: newest(groups[k]), reverse=True)
        editions[keys[0]] = len(keys)
        stale.update(keys[1:])
        notes.append(f"[series] {len(keys)} editions collapsed, kept: {groups[keys[0]][0]['title']}")

    nodes = []
    for key, members in groups.items():
        if key in stale:
            continue
        rep = max(members, key=lambda m: (m["score"], m["source_score"]))
        nodes.append({"items": members, "rep": rep, "editions": editions.get(key, 0)})

    # Type 2: similar wording, same news (word-overlap + time window)
    n = len(nodes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        ti = nodes[i]["rep"]["tokens"]
        if not ti:
            continue
        for j in range(i + 1, n):
            tj = nodes[j]["rep"]["tokens"]
            if not tj:
                continue
            if not within_window(nodes[i]["rep"]["dt"], nodes[j]["rep"]["dt"], d["window_hours"]):
                continue
            shared = len(ti & tj)
            if shared < d["min_shared_words"]:
                continue
            if shared / len(ti | tj) >= d["similarity_threshold"]:
                parent[find(j)] = find(i)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    results = []
    for idxs in clusters.values():
        cnodes = [nodes[i] for i in idxs]
        all_items = [it for nd in cnodes for it in nd["items"]]
        best = max(all_items, key=lambda m: (m["score"], m["source_score"]))
        best_pub = best["publisher"].strip().lower()
        pubs = {m["publisher"].strip().lower() for m in all_items}
        also = sorted({m["publisher"] for m in all_items
                       if m["publisher"].strip().lower() != best_pub})

        copies = len(all_items)
        if len(cnodes) > 1:
            rtype = "similar"
            titles = sorted({m["title"] for m in all_items})
            notes.append("[similar] " + str(len(titles)) + " titles merged:\n    - "
                         + "\n    - ".join(titles))
        elif copies > 1:
            rtype = "exact"
        else:
            rtype = ""

        boost = 0.0
        if best["relevant"] == "yes" and len(pubs) > 1:
            boost = min(d["boost_per_source"] * (len(pubs) - 1), d["max_boost"])

        results.append({
            "score": round(min(1.0, best["score"] + boost), 3),
            "base_score": best["score"],
            "relevant": best["relevant"],
            "direction": best["direction"],
            "publisher": best["publisher"],
            "title": best["title"],
            "url": best["url"],
            "published": best["published"],
            "relevancy": best["relevancy"],
            "source_score": best["source_score"],
            "buzz": best["buzz"],
            "matched": best["matched"],
            "repeat": copies,
            "repeat_sources": len(pubs),
            "repeat_type": rtype,
            "series_editions": max(nd["editions"] for nd in cnodes) or "",
            "also_reported_by": " | ".join(also),
            "dt": best["dt"],
        })
    return results, notes


# ---------- config / fetching / parsing ----------
def load_config(path=CONFIG_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Config file '{path}' not found. Create it next to News.py.")
    except json.JSONDecodeError as e:
        sys.exit(f"'{path}' is not valid JSON: {e}")

    if not cfg.get("feeds"):
        sys.exit(f"No feeds listed in '{path}'.")
    for key in ("topic", "weights"):
        if key not in cfg:
            sys.exit(f"'{path}' is missing the '{key}' section.")
    cfg.setdefault("source_trust", {})
    cfg.setdefault("default_trust", 0.3)
    cfg.setdefault("buzzwords", [])
    cfg.setdefault("min_score", 0)
    cfg.setdefault("irrelevant_penalty", 0.4)
    cfg.setdefault("prices", {})
    for k in ("iran", "asset", "crypto", "up", "down"):
        cfg["topic"].setdefault(k, [])

    # dedupe settings: numbers override defaults; stopwords/synonyms are ADDED to defaults
    user = cfg.get("dedupe", {})
    d = dict(DEFAULT_DEDUPE)
    d.update({k: v for k, v in user.items() if k not in ("stopwords", "synonyms")})
    d["stopwords"] = DEFAULT_DEDUPE["stopwords"] + user.get("stopwords", [])
    syn = {k: list(v) for k, v in DEFAULT_DEDUPE["synonyms"].items()}
    for canon, words in user.get("synonyms", {}).items():
        syn.setdefault(canon, []).extend(words)
    d["synonyms"] = syn
    cfg["dedupe"] = d
    return cfg


def fetch(url):
    # percent-encode any non-ASCII characters (e.g. Persian) but keep existing %XX
    url = urllib.parse.quote(url, safe=":/?&=+%@,;")
    req = urllib.request.Request(url, headers={"User-Agent": "NewsScrapper/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def clean(text):
    return " ".join(html.unescape(TAG_RE.sub(" ", text or "")).split())


def parse(xml_bytes):
    """Yield article dicts from RSS 2.0 or Atom."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        yield {
            "title": clean(item.findtext("title")),
            "link": (item.findtext("link") or "").strip(),
            "published": item.findtext("pubDate") or "",
            "summary": clean(item.findtext("description")),
            "publisher": clean(item.findtext("source")),  # set by Google News feeds
        }
    for entry in root.iter(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        yield {
            "title": clean(entry.findtext(ATOM + "title")),
            "link": link.get("href", "") if link is not None else "",
            "published": entry.findtext(ATOM + "updated") or "",
            "summary": clean(entry.findtext(ATOM + "summary")
                             or entry.findtext(ATOM + "content")),
            "publisher": "",
        }


def parse_time(text):
    """RSS (RFC 822) or Atom (ISO 8601) timestamp -> aware UTC datetime, or None."""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_prices(path):
    """Return (sorted snapshot times, matching row dicts) from the price log."""
    if not os.path.exists(path):
        return [], []
    snaps = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = parse_time(row.get("utc_time"))
            if t:
                snaps.append((t, row))
    snaps.sort(key=lambda s: s[0])
    return [s[0] for s in snaps], [s[1] for s in snaps]


def nearest_price(when, times, rows, max_gap_min):
    """Snapshot closest to 'when', or (None, None) if none within max_gap_min."""
    if when is None or not times:
        return None, None
    i = bisect.bisect_left(times, when)
    candidates = [j for j in (i - 1, i) if 0 <= j < len(times)]
    j = min(candidates, key=lambda k: abs((times[k] - when).total_seconds()))
    gap = (times[j] - when).total_seconds() / 60
    if abs(gap) > max_gap_min:
        return None, None
    return rows[j], round(gap, 1)


# ---------- scoring ----------
def find_hits(text, terms, whole=False):
    """Return the terms found in text.
    whole=True : term must be a complete word (plus a few suffixes)
    whole=False: term may be a word prefix (so 'surg' matches 'surging')"""
    hits = []
    for t in terms:
        t = t.lower()
        pattern = r"(?<!\w)" + re.escape(t)
        if whole:
            pattern += SUFFIX + r"(?!\w)"
        if re.search(pattern, text):
            hits.append(t)
    return hits


def score_article(art, feed_name, iran_local, cfg):
    topic = cfg["topic"]
    title = art["title"].lower()
    blob = f"{art['title']} {art['summary']}".lower()

    asset_terms = topic["asset"] + topic["crypto"]
    has_iran = iran_local or bool(find_hits(blob, topic["iran"], whole=True))
    asset_hits = find_hits(blob, asset_terms, whole=True)
    asset_in_title = bool(find_hits(title, asset_terms, whole=True))

    # Relevant = Iran context AND (money term in title OR two+ different money terms)
    relevant = has_iran and (asset_in_title or len(asset_hits) >= 2)

    up = len(find_hits(blob, topic["up"]))
    down = len(find_hits(blob, topic["down"]))
    movement = min(up + down, 2) / 2

    if relevant:
        relevancy = (
            0.3
            + 0.3 * min(len(asset_hits), 3) / 3
            + 0.2 * movement
            + (0.2 if asset_in_title else 0.0)
        )
    else:
        relevancy = 0.0

    publisher = (art["publisher"] or feed_name).lower()
    trust = cfg["default_trust"]
    for name, value in cfg["source_trust"].items():
        if name.lower() in publisher:
            trust = value
            break

    buzz = min(len(find_hits(blob, cfg["buzzwords"])), 3) / 3

    w = cfg["weights"]
    total = (w["relevancy"] * relevancy + w["source"] * trust + w["buzz"] * buzz) / sum(w.values())
    if not relevant:
        total *= cfg["irrelevant_penalty"]

    if up > down:
        direction = "up"
    elif down > up:
        direction = "down"
    else:
        direction = "mixed" if up else ""

    return {
        "score": round(total, 3),
        "relevant": "yes" if relevant else "no",
        "relevancy": round(relevancy, 3),
        "source_score": round(trust, 3),
        "buzz": round(buzz, 3),
        "direction": direction if relevant else "",
        "matched": "|".join(asset_hits),
    }


# ---------- main ----------
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Persian-safe on Windows
    cfg = load_config()

    prices_cfg = cfg["prices"]
    price_cols = list(prices_cfg.get("fields", {}).keys())
    price_file = prices_cfg.get("file", "output/prices.csv")
    max_gap = prices_cfg.get("max_gap_minutes", 30)
    snap_times, snap_rows = load_prices(price_file)
    if price_cols and not snap_times:
        print(f"[i] No price snapshots in '{price_file}' yet; price columns will be blank.")

    seen_urls = set()
    items = []

    for feed_name, spec in cfg["feeds"].items():
        if isinstance(spec, str):
            spec = {"url": spec}
        try:
            articles = list(parse(fetch(spec["url"])))
        except Exception as e:
            print(f"[!] {feed_name}: {e}")
            continue

        for art in articles:
            link = art["link"]
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)

            # Google News appends " - Publisher" to titles; strip it
            suffix = f" - {art['publisher']}"
            if art["publisher"] and art["title"].endswith(suffix):
                art["title"] = art["title"][: -len(suffix)]

            s = score_article(art, feed_name, spec.get("iran_local", False), cfg)
            items.append({
                **s,
                "publisher": art["publisher"] or feed_name,
                "title": art["title"],
                "url": link,
                "published": art["published"],
                "dt": parse_time(art["published"]),
            })

    results, notes = dedupe(items, cfg["dedupe"])

    rows, with_price = [], 0
    for r in results:
        if r["score"] < cfg["min_score"]:
            continue
        snap, gap = nearest_price(r["dt"], snap_times, snap_rows, max_gap)
        if snap:
            with_price += 1
        row = dict(r)
        row["published_utc"] = r["dt"].isoformat(timespec="seconds") if r["dt"] else ""
        row["price_gap_min"] = gap if gap is not None else ""
        for c in price_cols:
            row[c] = snap.get(c, "") if snap else ""
        rows.append(row)

    rows.sort(key=lambda r: r["score"], reverse=True)  # best score first

    if not rows:
        print("No articles found.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(OUTPUT_DIR, f"news_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=BASE_COLUMNS + price_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    for r in rows[:15]:
        tag = f" x{r['repeat']}" if r["repeat"] > 1 else ""
        print(f"{r['score']:.2f}{tag} [{r['relevant']:3}|{r['direction'] or '-':5}] "
              f"{r['publisher']}: {r['title']}")

    if SHOW_MERGES and notes:
        print("\n--- merges ---")
        for note in notes:
            print(note)

    print(f"\n{len(rows)} rows (from {len(items)} articles) saved to {path}")
    if price_cols:
        print(f"{with_price} matched a price snapshot")


if __name__ == "__main__":
    main()