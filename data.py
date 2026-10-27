"""Getting data in: feeds, article bodies, prices and labeling."""
import bisect
import csv
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from common import DIGITS, TAG_RE, clean, day, http_get, parse_time, score_article


# ---------------------------------------------------------------------- feeds

ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        yield {"title": clean(item.findtext("title")), "link": (item.findtext("link") or "").strip(),
               "published": item.findtext("pubDate") or "",
               "summary": clean(item.findtext("description")),
               "publisher": clean(item.findtext("source"))}
    for e in root.iter(ATOM + "entry"):
        link = e.find(ATOM + "link")
        yield {"title": clean(e.findtext(ATOM + "title")),
               "link": link.get("href", "") if link is not None else "",
               "published": e.findtext(ATOM + "updated") or "",
               "summary": clean(e.findtext(ATOM + "summary") or e.findtext(ATOM + "content")),
               "publisher": ""}


def spec_of(s):
    return {"url": s} if isinstance(s, str) else dict(s)


def make_jobs(cfg, d0, d1):
    if d0 is None:
        return [(n, spec_of(s)) for n, s in cfg["feeds"].items()]
    jobs, d = [], d0
    while d <= d1:
        for n, s in cfg["date_feeds"].items():
            s = spec_of(s)
            s["_name"] = n
            s["url"] = (s["url"].replace("{after}", d.isoformat())
                        .replace("{before}", (d + timedelta(days=1)).isoformat()))
            jobs.append((f"{n} {d}", s))
        d += timedelta(days=1)
    return jobs


def collect(db, cfg, d0=None, d1=None):
    jobs = make_jobs(cfg, d0, d1)
    if not jobs:
        sys.exit("No feeds to read. Add 'feeds' (live) or 'date_feeds' (with {after}/{before}).")
    new = new_rel = 0
    for name, spec in jobs:
        try:
            arts = list(parse_feed(http_get(spec["url"], cfg)[0]))
        except Exception as e:
            print(f"[!] {name}: {e}")
            continue
        for a in arts:
            if not a["link"] or not a["title"]:
                continue
            suffix = f" - {a['publisher']}"  # Google News appends " - Publisher"
            if a["publisher"] and a["title"].endswith(suffix):
                a["title"] = a["title"][: -len(suffix)]
            publisher = a["publisher"] or spec.get("_name", name)
            dt = parse_time(a["published"])
            il = bool(spec.get("iran_local", False))
            sc = score_article(a["title"], a["summary"], publisher, il, cfg)
            cur = db.execute(
                "INSERT OR IGNORE INTO articles(url,publisher,title,title_key,summary,"
                "published_utc,ts,relevant,matched,feed,score,relevancy,source_score,buzz,"
                "direction,series_key,had_date,iran_local) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["link"], publisher, a["title"], sc["title_key"], a["summary"],
                 dt.isoformat(timespec="seconds") if dt else "", int(dt.timestamp()) if dt else None,
                 sc["relevant"], sc["matched"], name, sc["score"], sc["relevancy"],
                 sc["source_score"], sc["buzz"], sc["direction"], sc["series_key"],
                 sc["had_date"], int(il)))
            new += cur.rowcount
            new_rel += cur.rowcount and sc["relevant"]
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"{new} new articles ({new_rel} relevant) from {len(jobs)} feed requests; {total} stored.")


# ---------------------------------------------------------------------- bodies

SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript",
             "svg", "iframe", "button"}


class BodyParser(HTMLParser):
    """Collect <p> text, ignoring menus/footers; paragraphs inside <article> win."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = self.art = 0
        self.in_p = False
        self.cur, self.paras, self.art_paras = [], [], []

    def flush(self):
        text = " ".join("".join(self.cur).split())
        self.cur, self.in_p = [], False
        if len(text) >= 30:
            self.paras.append(text)
            if self.art:
                self.art_paras.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "article":
            self.art += 1
        elif tag == "p":
            if self.in_p:
                self.flush()
            self.in_p = True
        elif tag == "br" and self.in_p:
            self.cur.append(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "article":
            if self.in_p:
                self.flush()
            self.art = max(0, self.art - 1)
        elif tag == "p" and self.in_p:
            self.flush()

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.cur.append(data)


def extract_body(raw, charset):
    try:
        page = raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        page = raw.decode("utf-8", errors="replace")
    p = BodyParser()
    p.feed(page)
    paras = p.art_paras or p.paras
    seen, out = set(), []
    for t in paras:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return "\n".join(out)[:20000]


_robots = {}


def robots_ok(url, cfg):
    u = urllib.parse.urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(http_get(base + "/robots.txt", cfg, 10)[0].decode("utf-8", "replace").splitlines())
        except Exception:
            rp = None  # no readable robots.txt -> allowed
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(cfg["user_agent"], url)


def real_url(url):
    """Google News links are wrappers; decode them if 'googlenewsdecoder' is installed."""
    if "news.google.com" not in url:
        return url
    try:
        import googlenewsdecoder as g  # pip install googlenewsdecoder (optional, Python 3.11+)
        fn = getattr(g, "gnewsdecoder", None) or getattr(g, "new_decoderv1", None)
        try:
            res = fn(url, interval=1)
        except TypeError:  # newer versions may not take 'interval'
            res = fn(url)
        if res.get("status") or res.get("success"):
            return res["decoded_url"]
    except Exception:
        pass
    return None


def scrape_bodies(db, cfg, limit, include_all=False):
    where = "" if include_all else "AND relevant=1"
    rows = db.execute(f"SELECT id,url FROM articles WHERE body_status='' {where} "
                      "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    stats = Counter()
    for r in rows:
        body, url = "", real_url(r["url"])
        if url is None:
            status = "gnews_link"
        elif not robots_ok(url, cfg):
            status = "robots"
        else:
            try:
                raw, cs = http_get(url, cfg, 25)
                body = extract_body(raw, cs)
                status = "ok" if len(body) >= 200 else "short"
            except Exception as e:
                status = f"error:{type(e).__name__}"
            time.sleep(cfg["scrape_delay"])
        stats[status.split(":")[0]] += 1
        db.execute("UPDATE articles SET body=?, body_status=? WHERE id=?", (body, status, r["id"]))
        db.commit()
    print(f"bodies: {dict(stats) or 'nothing to scrape'}")
    if stats["gnews_link"]:
        print("  Google News wrapper links can't be scraped as-is: use direct RSS feeds, "
              "or `pip install googlenewsdecoder`.")


# ---------------------------------------------------------------------- prices

def res_seconds(res):
    return 86400 if res.upper().endswith("D") else int(res) * 60


def fetch_candles_raw(cfg, t0, t1):
    m = cfg["market"]
    q = urllib.parse.urlencode({"symbol": m["symbol"], "resolution": m["resolution"],
                                "from": int(t0), "to": int(t1)})
    return json.loads(http_get(f"{m['base_url']}/market/udf/history?{q}", cfg,
                               ua=m["user_agent"])[0])


def fetch_candles(cfg, t0, t1):
    data = fetch_candles_raw(cfg, t0, t1)
    if data.get("s") != "ok":
        return []
    return list(zip(data["t"], data["o"], data["c"]))


def ensure_candles(db, cfg, start, end):
    m = cfg["market"]
    if not m.get("fetch", True):  # prices come from import-candles only
        return
    sym, res = m["symbol"], m["resolution"]
    lo, hi = db.execute("SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol=? AND res=?",
                        (sym, res)).fetchone()
    ranges = [(start, end)] if lo is None else (
        ([(start, lo)] if start < lo else []) + ([(hi, end)] if end > hi else []))
    step = min(m["chunk_days"] * 86400, 480 * res_seconds(res))  # API returns max 500 candles
    for a, b in ranges:
        t = a
        while t < b:
            e = min(t + step, b)
            try:
                rows = fetch_candles(cfg, t, e)
            except Exception as ex:
                print(f"[!] candles {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d}: {ex}")
                break
            db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?)",
                           [(sym, res, int(ts), o, c) for ts, o, c in rows])
            t = e
            time.sleep(1)
    db.commit()


class Prices:
    def __init__(self, db, cfg):
        m = cfg["market"]
        self.res = res_seconds(m["resolution"])
        rows = db.execute("SELECT ts,c FROM candles WHERE symbol=? AND res=? ORDER BY ts",
                          (m["symbol"], m["resolution"])).fetchall()
        self.ts, self.c = [r[0] for r in rows], [r[1] for r in rows]

    def at(self, t):
        """Close of the last candle that had finished by time t (never looks ahead)."""
        i = bisect.bisect_right(self.ts, t - self.res) - 1
        if i < 0 or t - (self.ts[i] + self.res) > self.res * 4:
            return None  # before our data, or a hole in the data
        return self.c[i]


def warn_fake(cfg):
    if str(cfg["market"]["symbol"]).upper().startswith("FAKE"):
        print("!! WARNING: market symbol is FAKE -> prices are random, results below are meaningless !!\n")


def label(db, cfg):
    warn_fake(cfg)
    rows = db.execute("SELECT id,ts FROM articles WHERE relevant=1 AND ts IS NOT NULL").fetchall()
    if not rows:
        print("label: no relevant articles yet.")
        return
    now = time.time()
    ensure_candles(db, cfg, min(r["ts"] for r in rows) - 86400, now)
    P = Prices(db, cfg)
    done = {(a, h) for a, h in db.execute("SELECT article_id,horizon FROM outcomes")}
    flat, added = cfg["flat_pct"], 0
    for r in rows:
        p0 = P.at(r["ts"])
        if p0 is None:
            continue
        for h in cfg["horizons_hours"]:
            t1 = r["ts"] + h * 3600
            if (r["id"], h) in done or t1 > now - P.res:
                continue
            p1 = P.at(t1)
            if p1 is None:
                continue
            pct = (p1 - p0) / p0 * 100
            d = "up" if pct > flat else "down" if pct < -flat else "flat"
            db.execute("INSERT OR REPLACE INTO outcomes VALUES(?,?,?,?,?,?)",
                       (r["id"], h, p0, p1, pct, d))
            added += 1
    db.commit()
    print(f"label: {added} new outcomes ({len(P.ts)} candles for {cfg['market']['symbol']}).")


def import_candles(db, cfg, path):
    """Load prices from your own CSV: columns 'time' (epoch seconds/ms or ISO) and 'close'
    (optional 'open'). Spacing must match market.resolution (e.g. hourly file -> "60")."""
    m = cfg["market"]
    rows, bad = [], 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = r.get("time") or r.get("timestamp") or r.get("utc_time") or r.get("date")
            c = r.get("close") or r.get("price") or r.get("latest")
            try:
                c = float(str(c).translate(DIGITS).replace(",", ""))
                o = float(r["open"]) if r.get("open") else c
                try:
                    ts = float(t)
                    ts = ts / 1000 if ts > 1e11 else ts
                except ValueError:
                    ts = parse_time(t).timestamp()
                rows.append((m["symbol"], m["resolution"], int(ts), o, c))
            except (TypeError, ValueError, AttributeError):
                bad += 1
    db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?)", rows)
    db.commit()
    print(f"imported {len(rows)} candles ({bad} unreadable rows) as {m['symbol']} / {m['resolution']}")


def fake_prices(db, cfg):
    """TEST ONLY. Random-walk candles stored under symbol 'FAKE' so the pipeline can be tried
    without a real price source. They are unrelated to the news, so results are meaningless."""
    import random
    res_txt = cfg["market"]["resolution"]
    res = res_seconds(res_txt)
    lo = db.execute("SELECT MIN(ts) FROM articles WHERE ts IS NOT NULL").fetchone()[0]
    now = int(time.time())
    start = (lo or now - 60 * 86400) - 2 * 86400
    start -= start % res
    rnd, price, rows = random.Random(42), 100000.0, []
    sigma = 0.0005 * math.sqrt(res / 900)
    for t in range(start, now + res, res):
        o = price
        price *= 1 + rnd.gauss(0, sigma)
        rows.append(("FAKE", res_txt, t, o, price))
    db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?)", rows)
    db.commit()
    print(f"{len(rows)} FAKE candles stored ({day(start)} .. today, resolution {res_txt}).\n"
          "Now set in config.json:  \"market\": { \"symbol\": \"FAKE\", \"fetch\": false, "
          f"\"resolution\": \"{res_txt}\" }}\nSwitch back to the real symbol (and remove 'fetch') "
          "once real prices work; the fake rows stay separate.")


def probe(cfg):
    now = time.time()
    try:
        data = fetch_candles_raw(cfg, now - 2 * 86400, now)
    except urllib.error.HTTPError as e:
        body = e.read(8000).decode("utf-8", "replace")
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        text = " ".join(html.unescape(TAG_RE.sub(" ", body)).split())
        sys.exit(f"HTTP {e.code} {e.reason}\nserver said: {text[:700]}")
    except Exception as e:
        sys.exit(f"Request failed: {e}")
    print(json.dumps({k: (v[:3] if isinstance(v, list) else v) for k, v in data.items()},
                     indent=2))

