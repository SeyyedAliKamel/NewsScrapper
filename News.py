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
BASE_COLUMNS = ["score", "relevant", "direction", "publisher", "title", "url",
                "published", "relevancy", "source_score", "buzz", "matched",
                "published_utc", "price_gap_min"]

ATOM = "{http://www.w3.org/2005/Atom}"
TAG_RE = re.compile(r"<[^>]+>")
SUFFIX = r"(?:s|es|ها|های|ی|ای)?"


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
    return cfg


def fetch(url):
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
            "publisher": clean(item.findtext("source")),
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
    """Snapshot closest to 'when', or (None, None) if none within max_gap_min.
    Gap is in minutes: negative = snapshot taken before the article."""
    if when is None or not times:
        return None, None
    i = bisect.bisect_left(times, when)
    candidates = [j for j in (i - 1, i) if 0 <= j < len(times)]
    j = min(candidates, key=lambda k: abs((times[k] - when).total_seconds()))
    gap = (times[j] - when).total_seconds() / 60
    if abs(gap) > max_gap_min:
        return None, None
    return rows[j], round(gap, 1)


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


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()

    prices_cfg = cfg["prices"]
    price_cols = list(prices_cfg.get("fields", {}).keys())
    price_file = prices_cfg.get("file", "output/prices.csv")
    max_gap = prices_cfg.get("max_gap_minutes", 30)
    snap_times, snap_rows = load_prices(price_file)
    if not snap_times:
        print(f"[i] No price snapshots in '{price_file}' yet; price columns will be blank.")

    seen = set()
    rows = []
    with_price = 0

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
            if not link or link in seen:
                continue
            seen.add(link)

            suffix = f" - {art['publisher']}"
            if art["publisher"] and art["title"].endswith(suffix):
                art["title"] = art["title"][: -len(suffix)]

            s = score_article(art, feed_name, spec.get("iran_local", False), cfg)
            if s["score"] < cfg["min_score"]:
                continue

            published_dt = parse_time(art["published"])
            snap, gap = nearest_price(published_dt, snap_times, snap_rows, max_gap)
            if snap:
                with_price += 1

            rows.append([
                s["score"], s["relevant"], s["direction"], art["publisher"] or feed_name,
                art["title"], link, art["published"],
                s["relevancy"], s["source_score"], s["buzz"], s["matched"],
                published_dt.isoformat(timespec="seconds") if published_dt else "",
                gap if gap is not None else "",
            ] + [snap.get(c, "") if snap else "" for c in price_cols])

    rows.sort(key=lambda r: r[0], reverse=True)

    if not rows:
        print("No articles found.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(OUTPUT_DIR, f"news_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(BASE_COLUMNS + price_cols)
        writer.writerows(rows)

    for r in rows[:15]:
        print(f"{r[0]:.2f} [{r[1]:3}|{r[2] or '-':5}] {r[3]}: {r[4]}")
    print(f"\n{len(rows)} articles saved to {path} ({with_price} matched a price snapshot)")


if __name__ == "__main__":
    main()