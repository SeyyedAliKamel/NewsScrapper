"""Shared base: config, database, text helpers, scoring."""
import html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# ---------------------------------------------------------------------- util

TAG_RE = re.compile(r"<[^>]+>")


def http_get(url, cfg, timeout=20, ua=None):
    url = urllib.parse.quote(url, safe=":/?&=+%@,;#~")
    req = urllib.request.Request(url, headers={"User-Agent": ua or cfg["user_agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000), r.headers.get_content_charset()


def clean(text):
    return " ".join(html.unescape(TAG_RE.sub(" ", text or "")).split())


def parse_time(text):
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


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------- text

SUFFIX = r"(?:s|es|ها|های|ی|ای)?"

PERSIAN_FIX = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",
                             "\u200c": " ", "\u200f": None, "\u200e": None})
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize(text):
    text = (text or "").translate(PERSIAN_FIX).translate(DIGITS).lower()
    text = re.sub(r"(?<=\d)[,٬](?=\d{3})", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def stem(w):
    for suf in ("های", "ها"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    return w[:5]  # crude: rise/rises/rising -> same 5-letter prefix


def tokens(text, cfg):
    out = []
    for w in normalize(text).split():
        if w in cfg["_stop"] or w.isdigit() or len(w) < 2:
            continue
        mapped = next((c for p, c in cfg["_syn"] if w.startswith(p)), None)
        out.append(mapped or stem(w))
    return out


def find_hits(text, terms, whole=False):
    hits = []
    for t in terms:
        pattern = r"(?<!\w)" + re.escape(t.lower()) + (SUFFIX + r"(?!\w)" if whole else "")
        if re.search(pattern, text):
            hits.append(t.lower())
    return hits


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


def make_template(norm):
    """Remove dates so daily editions of one headline get the same key (a 'series')."""
    out, had = norm, False
    for rx in DATE_RES + [RELATIVE_RE]:
        out, n = rx.subn(" ", out)
        had = had or n > 0
    return " ".join(out.split()), int(had)


# ---------------------------------------------------------------------- config

CONFIG_FILE = "config.json"
DB_FILE = "output/news.db"
EXPORT_FILE = "output/news_labeled.csv"

DEFAULTS = {
    "user_agent": "Mozilla/5.0 (compatible; NewsScrapper/0.2)",
    "market": {"base_url": "https://apiv2.nobitex.ir", "symbol": "USDTIRT",
               "resolution": "15", "chunk_days": 5,
               "user_agent": "TraderBot/NewsPredictor-0.2"},  # Nobitex asks bots to use TraderBot/<name>-<version>
    "horizons_hours": [1, 6, 24],
    "flat_pct": 0.1,          # moves smaller than this (%) count as "flat"
    "scrape_delay": 1.5,      # seconds between article downloads
    "predict": {
        "min_similarity": 0.25,    # a past article must be at least this similar (0-1)
        "k": 8,                    # use at most this many neighbours per story
        "min_matches": 3,          # need at least this many neighbours or no vote
        "cluster_similarity": 0.6, # today's articles this similar = one story = one vote
        "title_weight": 3,         # title words count this many times
        "body_chars": 3000,        # only the start of the body is compared
        "use_scores": True,        # past/today articles count in proportion to their score
    },
    "source_trust": {}, "default_trust": 0.3, "buzzwords": [],
    "weights": {"relevancy": 0.5, "source": 0.3, "buzz": 0.2},
    "irrelevant_penalty": 0.4, "min_score": 0,
    "dedupe": {"window_hours": 48, "boost_per_source": 0.05, "max_boost": 0.2},
    "feeds": {},
    "date_feeds": {},
    "topic": {},
}

STOPWORDS = [
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "by", "from",
    "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
    "those", "after", "over", "amid", "says", "said", "say", "new", "news", "latest", "update",
    "live", "report", "reports", "reported", "will", "has", "have", "had", "not", "but", "than",
    "into", "about", "up", "down", "out", "who", "what", "why", "how", "s", "t",
    "از", "به", "در", "با", "که", "را", "این", "آن", "و", "برای", "تا", "بر", "هم", "می",
    "شد", "شده", "است", "بود", "خواهد", "یک", "هر", "نیز", "اما", "یا", "پس",
]
SYNONYMS = {  # canonical word -> word prefixes that count as the same thing
    "fall": ["fall", "fell", "drop", "plung", "plummet", "slump", "tumbl", "slid", "sink", "sank",
             "crash", "declin", "weaken", "slip", "کاهش", "سقوط", "ریزش", "افت"],
    "rise": ["rise", "rising", "rose", "surg", "soar", "jump", "climb", "rall", "spik", "gain",
             "hike", "leap", "افزایش", "صعود", "جهش", "رشد"],
    "usd": ["dollar", "usd", "greenback", "دلار"],
    "rial": ["rial", "toman", "ریال", "تومان"],
    "iran": ["iran", "tehran", "ایران", "تهران"],
    "gold": ["gold", "bullion", "طلا"],
    "coin": ["coin", "سکه"],
    "crypto": ["crypto", "bitcoin", "btc", "tether", "usdt", "رمزارز", "تتر"],
    "price": ["price", "rate", "قیمت", "نرخ"],
}


def load_config(path=CONFIG_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Config file '{path}' not found.")
    except json.JSONDecodeError as e:
        sys.exit(f"'{path}' is not valid JSON: {e}")
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and k in ("market", "predict", "topic", "weights", "dedupe"):
            cfg[k].update(v)
        else:
            cfg[k] = v
    for k in ("iran", "asset", "crypto", "up", "down"):
        cfg["topic"].setdefault(k, [])
    d = user.get("dedupe", {})  # same stopwords/synonyms extension block as before
    stop = STOPWORDS + d.get("stopwords", [])
    syn = {k: list(v) for k, v in SYNONYMS.items()}
    for canon, words in d.get("synonyms", {}).items():
        syn.setdefault(canon, []).extend(words)
    cfg["_stop"] = {normalize(w) for w in stop}
    cfg["_syn"] = sorted(((normalize(p), c) for c, ps in syn.items() for p in ps),
                         key=lambda x: -len(x[0]))
    return cfg


# ---------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, publisher TEXT, title TEXT, title_key TEXT,
  summary TEXT, body TEXT, body_status TEXT DEFAULT '', published_utc TEXT, ts INTEGER,
  relevant INTEGER, matched TEXT, feed TEXT, UNIQUE(publisher, title_key));
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT, res TEXT, ts INTEGER, o REAL, c REAL, PRIMARY KEY(symbol, res, ts));
CREATE TABLE IF NOT EXISTS outcomes(
  article_id INTEGER, horizon INTEGER, price0 REAL, price1 REAL, pct REAL, direction TEXT,
  PRIMARY KEY(article_id, horizon));
"""

NEW_COLS = {"score": "REAL", "relevancy": "REAL", "source_score": "REAL", "buzz": "REAL",
            "direction": "TEXT", "series_key": "TEXT", "had_date": "INTEGER", "iran_local": "INTEGER"}


def open_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(articles)")}
    for col, typ in NEW_COLS.items():  # upgrade databases made by the earlier version
        if col not in have:
            db.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
    db.commit()
    return db


def all_rows(db, cfg, relevant_only=False):
    q = "SELECT * FROM articles WHERE ts IS NOT NULL AND (score IS NULL OR score >= ?)"
    if relevant_only:
        q += " AND relevant=1"
    return db.execute(q + " ORDER BY ts", (cfg["min_score"],)).fetchall()


def load_outcomes(db):
    return {(a, h): (pct, d) for a, h, pct, d in
            db.execute("SELECT article_id,horizon,pct,direction FROM outcomes")}


# ---------------------------------------------------------------------- scoring

def score_article(title, summary, publisher, iran_local, cfg, force_relevant=None):
    t = cfg["topic"]
    blob = f"{title} {summary}".lower()
    asset_terms = t["asset"] + t["crypto"]
    has_iran = iran_local or bool(find_hits(blob, t["iran"], True))
    asset_hits = find_hits(blob, asset_terms, True)
    in_title = bool(find_hits(title.lower(), asset_terms, True))
    relevant = has_iran and (in_title or len(asset_hits) >= 2)
    if force_relevant is not None:
        relevant = bool(force_relevant)
    up, down = len(find_hits(blob, t["up"])), len(find_hits(blob, t["down"]))
    movement = min(up + down, 2) / 2
    relevancy = (0.3 + 0.3 * min(len(asset_hits), 3) / 3 + 0.2 * movement
                 + (0.2 if in_title else 0.0)) if relevant else 0.0
    trust, pub = cfg["default_trust"], (publisher or "").lower()
    for name, value in cfg["source_trust"].items():
        if name.lower() in pub:
            trust = value
            break
    buzz = min(len(find_hits(blob, cfg["buzzwords"])), 3) / 3
    w = cfg["weights"]
    total = (w["relevancy"] * relevancy + w["source"] * trust + w["buzz"] * buzz) / sum(w.values())
    if not relevant:
        total *= cfg["irrelevant_penalty"]
    direction = "up" if up > down else "down" if down > up else ("mixed" if up else "")
    norm = normalize(title)
    series_key, had_date = make_template(norm)
    return {"score": round(total, 3), "relevant": int(relevant), "relevancy": round(relevancy, 3),
            "source_score": round(trust, 3), "buzz": round(buzz, 3),
            "direction": direction if relevant else "", "matched": "|".join(asset_hits),
            "title_key": norm, "series_key": series_key, "had_date": had_date}


def rescore(db, cfg):
    """Recompute every score from stored text, e.g. after editing weights, trust or topic terms."""
    rows = db.execute("SELECT id,title,summary,publisher,iran_local,relevant FROM articles").fetchall()
    for r in rows:
        known = r["iran_local"] is not None  # older rows: keep the stored relevant flag
        sc = score_article(r["title"], r["summary"], r["publisher"], bool(r["iran_local"]), cfg,
                           None if known else r["relevant"])
        db.execute("UPDATE articles SET score=?,relevancy=?,source_score=?,buzz=?,direction=?,"
                   "relevant=?,matched=?,series_key=?,had_date=?,title_key=? WHERE id=?",
                   (sc["score"], sc["relevancy"], sc["source_score"], sc["buzz"], sc["direction"],
                    sc["relevant"], sc["matched"], sc["series_key"], sc["had_date"],
                    sc["title_key"], r["id"]))
    db.commit()
    print(f"rescore: {len(rows)} articles.")

