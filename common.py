"""Shared base: config, database, text helpers, scoring."""
import html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
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


# ---------------------------------------------------------------------- temporal event extraction

IRST = timezone(timedelta(hours=3, minutes=30))


def extract_event_time(text, pub_dt=None, is_iran_local=False):
    """
    Extract in-text event time clues from article title/summary/body.
    Detects if a statement or event occurred at a different time than published_utc.
    Returns (event_ts, event_utc_iso, event_clue, lag_hours) or (None, None, "", None).
    """
    if not text:
        return None, None, "", None

    raw = (text or "").translate(PERSIAN_FIX).translate(DIGITS)

    if pub_dt is None:
        pub_dt = datetime.now(timezone.utc)
    elif pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    else:
        pub_dt = pub_dt.astimezone(timezone.utc)

    pub_ts = int(pub_dt.timestamp())

    # 1. English explicit GMT / UTC time: e.g. "06:36 GMT", "at 14:00 UTC"
    m_gmt = re.search(r'\b(?:at\s+)?([0-1]?[0-9]|2[0-3]):([0-5][0-9])\s*(?:gmt|utc)\b', raw, re.IGNORECASE)
    if m_gmt:
        h, m = int(m_gmt.group(1)), int(m_gmt.group(2))
        ev_dt = pub_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if ev_dt > pub_dt:
            ev_dt -= timedelta(days=1)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            clue = f"{h:02d}:{m:02d} GMT"
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), clue, round(lag, 2)

    # 2. English ET / EST / EDT time: e.g. "02:36 ET", "at 9:00 AM ET"
    m_et = re.search(r'\b(?:at\s+)?([0-1]?[0-9]|2[0-3]):([0-5][0-9])\s*(?:am|pm)?\s*(?:et|est|edt)\b', raw, re.IGNORECASE)
    if m_et:
        h, m = int(m_et.group(1)), int(m_et.group(2))
        et_offset = timedelta(hours=-4)
        et_tz = timezone(et_offset)
        pub_et = pub_dt.astimezone(et_tz)
        is_pm = 'pm' in m_et.group(0).lower()
        if is_pm and h < 12:
            h += 12
        elif 'am' in m_et.group(0).lower() and h == 12:
            h = 0
        ev_local = pub_et.replace(hour=h, minute=m, second=0, microsecond=0)
        if ev_local > pub_et:
            ev_local -= timedelta(days=1)
        ev_dt = ev_local.astimezone(timezone.utc)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            clue = f"{m_et.group(0).strip()}"
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), clue, round(lag, 2)

    # 3. Persian Clock Time: e.g. "ساعت ۱۲ ظهر", "ساعت ۹:۳۰ صبح", "ساعت ۱۴", "ساعت ۲ بعد از ظهر"
    # IRST is UTC+03:30
    m_fa_clock = re.search(r'(?:ساعت|در\s+ساعت|حوالی\s+ساعت|راس\s+ساعت)\s*([0-2]?[0-9])(?::([0-5][0-9]))?\s*(صبح|ظهر|عصر|شب|بامداد|بعد\s*از\s*ظهر|قبل\s*از\s*ظهر)?', raw)
    if m_fa_clock:
        h = int(m_fa_clock.group(1))
        m = int(m_fa_clock.group(2)) if m_fa_clock.group(2) else 0
        modifier = (m_fa_clock.group(3) or "").replace(" ", "")

        if 0 <= h <= 24:
            if modifier in ("عصر", "شب", "بعدازظهر") and h < 12:
                h += 12
            elif modifier == "ظهر" and h < 11:
                h += 12
            elif modifier in ("صبح", "بامداد") and h == 12:
                h = 0

            if h <= 23:
                pub_local = pub_dt.astimezone(IRST)
                ev_local = pub_local.replace(hour=h, minute=m, second=0, microsecond=0)
                if ev_local > pub_local:
                    ev_local -= timedelta(days=1)
                ev_dt = ev_local.astimezone(timezone.utc)
                lag = (pub_ts - ev_dt.timestamp()) / 3600.0
                if 0.0 <= lag <= 48.0:
                    clue = f"ساعت {h:02d}:{m:02d} (ایران)"
                    return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), clue, round(lag, 2)

    # 4. English 12h clock time: e.g. "at 12:30 pm", "at 9 am"
    m_en_clock = re.search(r'\b(?:at|around)\s+([0-1]?[0-9])(?::([0-5][0-9]))?\s*(am|pm|a\.m\.|p\.m\.)\b', raw, re.IGNORECASE)
    if m_en_clock:
        h = int(m_en_clock.group(1))
        m = int(m_en_clock.group(2)) if m_en_clock.group(2) else 0
        ampm = m_en_clock.group(3).lower().replace(".", "")
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        tz = IRST if is_iran_local else timezone.utc
        pub_local = pub_dt.astimezone(tz)
        ev_local = pub_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if ev_local > pub_local:
            ev_local -= timedelta(days=1)
        ev_dt = ev_local.astimezone(timezone.utc)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            clue = f"{h:02d}:{m:02d} {'IRST' if is_iran_local else 'UTC'}"
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), clue, round(lag, 2)

    # 5. Persian Relative Clues:
    if re.search(r'\bدقایقی\s+پیش\b', raw):
        ev_dt = pub_dt - timedelta(minutes=15)
        return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), "دقایقی پیش (~15 دقیقه)", 0.25

    if re.search(r'\bساعاتی\s+پیش\b', raw):
        ev_dt = pub_dt - timedelta(hours=2)
        return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), "ساعاتی پیش (~2 ساعت)", 2.0

    if re.search(r'\b(?:صبح\s+امروز|امروز\s+صبح)\b', raw):
        pub_local = pub_dt.astimezone(IRST)
        ev_local = pub_local.replace(hour=9, minute=30, second=0, microsecond=0)
        if ev_local > pub_local:
            ev_local = pub_local
        ev_dt = ev_local.astimezone(timezone.utc)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), "صبح امروز (~09:30 IRST)", round(lag, 2)

    if re.search(r'\b(?:ظهر\s+امروز|امروز\s+ظهر)\b', raw):
        pub_local = pub_dt.astimezone(IRST)
        ev_local = pub_local.replace(hour=12, minute=30, second=0, microsecond=0)
        if ev_local > pub_local:
            ev_local = pub_local
        ev_dt = ev_local.astimezone(timezone.utc)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), "ظهر امروز (~12:30 IRST)", round(lag, 2)

    if re.search(r'\b(?:دیشب|شب\s+گذشته)\b', raw):
        pub_local = pub_dt.astimezone(IRST)
        ev_local = (pub_local - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        ev_dt = ev_local.astimezone(timezone.utc)
        lag = (pub_ts - ev_dt.timestamp()) / 3600.0
        if 0.0 <= lag <= 48.0:
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), "دیشب (~21:00 IRST)", round(lag, 2)

    # 6. English Relative Clues: e.g. "X hours ago", "X mins ago"
    m_en_rel = re.search(r'\b([0-9]{1,2})\s*(?:hours?|hrs?)\s*ago\b', raw, re.IGNORECASE)
    if m_en_rel:
        hrs = int(m_en_rel.group(1))
        if 0 < hrs <= 48:
            ev_dt = pub_dt - timedelta(hours=hrs)
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), f"{hrs}h ago", float(hrs)

    m_en_rel_m = re.search(r'\b([0-9]{1,2})\s*(?:minutes?|mins?)\s*ago\b', raw, re.IGNORECASE)
    if m_en_rel_m:
        mins = int(m_en_rel_m.group(1))
        if 0 < mins <= 120:
            ev_dt = pub_dt - timedelta(minutes=mins)
            return int(ev_dt.timestamp()), ev_dt.strftime("%Y-%m-%d %H:%M"), f"{mins}m ago", round(mins / 60.0, 2)

    return None, None, "", None


# ---------------------------------------------------------------------- config

CONFIG_FILE = "config.json"
DB_FILE = "output/news.db"
EXPORT_FILE = "output/news_labeled.csv"

DEFAULTS = {
    "user_agent": "Mozilla/5.0 (compatible; NewsScrapper/0.2)",
    "markets": {
        "dollar": {
            "name": "US Dollar (دلار)", "symbol": "USD_IRT", "provider": "tgju",
            "indicator": "price_dollar_rl", "resolution": "D", "unit": "Rial",
            "keywords": ["dollar", "usd", "rial", "exchange rate", "currency", "forex", "bonbast", "دلار", "ریال", "صرافی", "ارز"]
        },
        "gold": {
            "name": "Gold & Coin (سکه و طلا)", "symbol": "GOLD_SEKEE", "provider": "tgju",
            "indicator": "sekee", "resolution": "D", "unit": "Rial",
            "keywords": ["gold", "coin", "bahar azadi", "sekkeh", "طلا", "سکه", "امامی", "بهار آزادی", "mesghal", "مثقال", "گرمی", "gerami", "paxg"]
        },
        "btc": {
            "name": "Bitcoin (بیت کوین)", "symbol": "BTCIRT", "provider": "nobitex",
            "resolution": "15", "unit": "Toman",
            "keywords": ["bitcoin", "btc", "crypto", "ethereum", "cryptocurrency", "stablecoin", "tether", "usdt", "nobitex", "binance", "بیت کوین", "بیت‌کوین", "بیتکوین", "تتر", "رمزارز", "ارز دیجیتال", "نوبیتکس", "کریپتو"]
        }
    },
    "active_market": "all",
    "market": {"base_url": "https://apiv2.nobitex.ir", "symbol": "BTCIRT",
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
        if isinstance(v, dict) and k in ("market", "markets", "predict", "topic", "weights", "dedupe"):
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
  article_id INTEGER, symbol TEXT, horizon INTEGER, price0 REAL, price1 REAL, pct REAL, direction TEXT,
  PRIMARY KEY(article_id, symbol, horizon));
"""

NEW_COLS = {"score": "REAL", "relevancy": "REAL", "source_score": "REAL", "buzz": "REAL",
            "direction": "TEXT", "series_key": "TEXT", "had_date": "INTEGER", "iran_local": "INTEGER", "market_tag": "TEXT", "event_ts": "INTEGER", "event_utc": "TEXT", "event_clue": "TEXT", "event_lag_hours": "REAL"}


def open_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(articles)")}
    for col, typ in NEW_COLS.items():  # upgrade databases made by the earlier version
        if col not in have:
            db.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
    out_cols = {r[1] for r in db.execute("PRAGMA table_info(outcomes)")}
    if "symbol" not in out_cols:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS outcomes_v2(
              article_id INTEGER, symbol TEXT, horizon INTEGER, price0 REAL, price1 REAL, pct REAL, direction TEXT,
              PRIMARY KEY(article_id, symbol, horizon));
            INSERT OR IGNORE INTO outcomes_v2(article_id, symbol, horizon, price0, price1, pct, direction)
              SELECT article_id, 'FAKE', horizon, price0, price1, pct, direction FROM outcomes;
            DROP TABLE outcomes;
            ALTER TABLE outcomes_v2 RENAME TO outcomes;
        """)
    db.commit()
    return db


def all_rows(db, cfg, relevant_only=False):
    q = "SELECT * FROM articles WHERE ts IS NOT NULL AND (score IS NULL OR score >= ?)"
    if relevant_only:
        q += " AND relevant=1"
    return db.execute(q + " ORDER BY ts", (cfg["min_score"],)).fetchall()


def load_outcomes(db, symbol=None):
    if symbol:
        return {(a, h): (pct, d) for a, h, pct, d in
                db.execute("SELECT article_id,horizon,pct,direction FROM outcomes WHERE symbol=?", (symbol,))}
    res = {}
    for a, s, h, pct, d in db.execute("SELECT article_id,symbol,horizon,pct,direction FROM outcomes"):
        res.setdefault((a, h), (pct, d))
    return res


# ---------------------------------------------------------------------- scoring

def detect_markets(text, cfg):
    mkts = cfg.get("markets", {})
    if not mkts:
        return "macro"
    hits = []
    blob = text.lower()
    for m_key, m_info in mkts.items():
        kws = m_info.get("keywords", [])
        if find_hits(blob, kws, whole=True):
            hits.append(m_key)
    return "|".join(hits) if hits else "macro"


def score_article(title, summary, publisher, iran_local, cfg, force_relevant=None, pub_dt=None, body=None):
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
    ev_ts, ev_utc, ev_clue, ev_lag = extract_event_time(f"{title} {summary} {body or ''}", pub_dt, iran_local)
    return {"score": round(total, 3), "relevant": int(relevant), "relevancy": round(relevancy, 3),
            "source_score": round(trust, 3), "buzz": round(buzz, 3),
            "direction": direction if relevant else "", "matched": "|".join(asset_hits),
            "title_key": norm, "series_key": series_key, "had_date": had_date, "market_tag": detect_markets(f"{title} {summary}", cfg),
            "event_ts": ev_ts, "event_utc": ev_utc, "event_clue": ev_clue, "event_lag_hours": ev_lag}


def rescore(db, cfg):
    """Recompute every score and in-text event time from stored text."""
    rows = db.execute("SELECT id,title,summary,body,publisher,iran_local,relevant,published_utc FROM articles").fetchall()
    for r in rows:
        known = r["iran_local"] is not None  # older rows: keep the stored relevant flag
        pub_dt = parse_time(r["published_utc"])
        sc = score_article(r["title"], r["summary"], r["publisher"], bool(r["iran_local"]), cfg,
                           None if known else r["relevant"], pub_dt=pub_dt, body=r["body"])
        db.execute("UPDATE articles SET score=?,relevancy=?,source_score=?,buzz=?,direction=?,"
                   "relevant=?,matched=?,series_key=?,had_date=?,title_key=?,market_tag=?,"
                   "event_ts=?,event_utc=?,event_clue=?,event_lag_hours=? WHERE id=?",
                   (sc["score"], sc["relevancy"], sc["source_score"], sc["buzz"], sc["direction"],
                    sc["relevant"], sc["matched"], sc["series_key"], sc["had_date"],
                    sc["title_key"], sc["market_tag"],
                    sc["event_ts"], sc["event_utc"], sc["event_clue"], sc["event_lag_hours"], r["id"]))
    db.commit()
    print(f"rescore: {len(rows)} articles.")

