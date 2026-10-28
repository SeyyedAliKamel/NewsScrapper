"""Using the data: similarity, prediction, backtest, top list, CSV export, doctor."""
import csv
import math
import os
import time
from collections import Counter, defaultdict

from common import EXPORT_FILE, DB_FILE, all_rows, day, load_outcomes, tokens
from data import warn_fake


# ---------------------------------------------------------------------- similarity

class Index:
    """TF-IDF vectors + inverted index. Cosine similarity."""

    def __init__(self, rows, cfg):
        p = cfg["predict"]
        self.rows = {r["id"]: r for r in rows}
        toks = {}
        for r in rows:
            body = (r["body"] or r["summary"] or "")[: p["body_chars"]]
            toks[r["id"]] = tokens(r["title"], cfg) * p["title_weight"] + tokens(body, cfg)
        df = Counter()
        for t in toks.values():
            df.update(set(t))
        n = len(toks)
        self.idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
        self.vec = {i: self._vec(t) for i, t in toks.items()}
        self.post = defaultdict(list)
        for i, v in self.vec.items():
            for w, x in v.items():
                self.post[w].append((i, x))

    def _vec(self, toks):
        v = {w: (1 + math.log(c)) * self.idf[w] for w, c in Counter(toks).items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    def sim(self, i, j):
        a, b = self.vec[i], self.vec[j]
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(w, 0.0) for w, x in a.items())

    def similar(self, i, ok):
        scores = defaultdict(float)
        for w, x in self.vec[i].items():
            for j, y in self.post.get(w, ()):
                scores[j] += x * y
        return sorted(((s, j) for j, s in scores.items() if j != i and ok(j)), reverse=True)


def story_groups(idx, ids, cfg):
    """Merge articles about the same news: similar wording AND published close together."""
    thr = cfg["predict"]["cluster_similarity"]
    win = cfg["dedupe"]["window_hours"] * 3600
    idset, parent = set(ids), {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in ids:
        for s, j in idx.similar(i, lambda j: j in idset):
            if s < thr:
                break
            if abs(idx.rows[j]["ts"] - idx.rows[i]["ts"]) <= win:
                parent[find(j)] = find(i)
    groups = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return list(groups.values())


def story_info(idx, g, cfg):
    """Best article of a story, its outlets and the 'more outlets = more credible' boost."""
    rows = [idx.rows[i] for i in g]
    best = max(rows, key=lambda r: (r["score"] or 0, r["source_score"] or 0))
    pubs = {r["publisher"].strip().lower() for r in rows}
    boost = 0.0
    if best["relevant"] and len(pubs) > 1:
        d = cfg["dedupe"]
        boost = min(d["boost_per_source"] * (len(pubs) - 1), d["max_boost"])
    also = sorted({r["publisher"] for r in rows
                   if r["publisher"].strip().lower() != best["publisher"].strip().lower()})
    rtype = "" if len(rows) == 1 else ("exact" if len({r["title_key"] for r in rows}) == 1 else "similar")
    return {"best": best, "score": round(min(1.0, (best["score"] or 0) + boost), 3),
            "repeat": len(rows), "repeat_sources": len(pubs), "repeat_type": rtype,
            "also": " | ".join(also), "rows": rows}


def annotate(rows, cfg):
    """Collapse duplicates/same-news into stories with repeat counts."""
    idx = Index(rows, cfg)
    series = Counter(r["series_key"] for r in rows if r["had_date"])
    stories, notes = [], []
    for g in story_groups(idx, [r["id"] for r in rows], cfg):
        st = story_info(idx, g, cfg)
        key = st["best"]["series_key"]
        st["editions"] = series[key] if st["best"]["had_date"] and series[key] > 1 else ""
        if st["repeat_type"] == "similar":
            titles = sorted({r["title"] for r in st["rows"]})
            notes.append(f"[similar] {len(titles)} titles merged:\n    - " + "\n    - ".join(titles))
        stories.append(st)
    return stories, notes


# ---------------------------------------------------------------------- predict

def vote(idx, art_id, h, outcomes, cfg, known_by):
    """Weighted vote of similar PAST articles whose +h outcome was already known at `known_by`.
    One neighbour per series/story, so a story covered 10 times is not counted 10 times."""
    p = cfg["predict"]
    win = cfg["dedupe"]["window_hours"] * 3600
    ok = lambda j: (j, h) in outcomes and idx.rows[j]["ts"] + h * 3600 <= known_by
    picked = []
    for s, j in idx.similar(art_id, ok):
        if s < p["min_similarity"]:
            break
        rj = idx.rows[j]
        if any(rj["series_key"] == idx.rows[q]["series_key"]
               or (idx.sim(j, q) >= p["cluster_similarity"]
                   and abs(rj["ts"] - idx.rows[q]["ts"]) <= win) for _, q in picked):
            continue
        picked.append((s, j))
        if len(picked) == p["k"]:
            break
    if len(picked) < p["min_matches"]:
        return None
    neigh = [(s * ((idx.rows[j]["score"] or 0.0) if p["use_scores"] else 1.0), s, j)
             for s, j in picked]
    W = sum(w for w, _, _ in neigh)
    if W <= 0:
        return None
    share = lambda d: sum(w for w, _, j in neigh if outcomes[(j, h)][1] == d) / W
    return {"pct": sum(w * outcomes[(j, h)][0] for w, _, j in neigh) / W, "up": share("up"),
            "down": share("down"), "weight": W, "neigh": neigh}


def _predict_single_market(db, cfg, hours, m_key, m_info, all_rows_list):
    sym = m_info["symbol"]
    name = m_info.get("name", sym)
    p = cfg["predict"]
    now = time.time()
    
    # Filter rows matching this market or macro
    market_rows = [r for r in all_rows_list if not r["market_tag"] or m_key in r["market_tag"].split("|") or r["market_tag"] == "macro"]
    if not market_rows:
        return
    recent = [r["id"] for r in market_rows if r["ts"] >= now - hours * 3600]
    outcomes = load_outcomes(db, symbol=sym)
    if not outcomes:
        print(f"\n[{name} ({sym})]: No labeled history found. Run `python News.py label` first.")
        return

    idx = Index(market_rows, cfg)
    groups = story_groups(idx, recent, cfg)
    print(f"\n==================================================================")
    print(f"  MARKET: {name} (Symbol: {sym})")
    print(f"  Recent stories in {hours}h: {len(recent)} ({len(groups)} distinct) | Labeled history: {len({a for (a,h) in outcomes})} stories")
    print(f"==================================================================")
    
    if not recent:
        print(f"   No relevant {name} news in the last {hours}h.")
        return

    for h in cfg["horizons_hours"]:
        pool = [o for (a, hh), o in outcomes.items() if hh == h]
        if not pool:
            continue
        n = len(pool)
        up_cnt = sum(o[1] == "up" for o in pool)
        dn_cnt = sum(o[1] == "down" for o in pool)
        print(f"\n  -- Horizon +{h}h (History base rate: {up_cnt/n:.0%} UP / {dn_cnt/n:.0%} DOWN on {n} events) --")
        votes = []
        for g in groups:
            st = story_info(idx, g, cfg)
            c = [(vote(idx, i, h, outcomes, cfg, now), i) for i in g]
            c = [(v, i) for v, i in c if v]
            if c:
                v, i = max(c, key=lambda x: x[0]["weight"])
                votes.append((v, i, st, v["weight"] * (st["score"] if p["use_scores"] else 1.0)))
        if not votes:
            print("     No story has enough similar past evidence -> no signal.")
            continue
        W = sum(x[3] for x in votes)
        pct = sum(x[3] * x[0]["pct"] for x in votes) / W
        up = sum(x[3] * x[0]["up"] for x in votes) / W
        dn = sum(x[3] * x[0]["down"] for x in votes) / W
        fl = max(0.0, 1 - up - dn)
        call = max((up, "UP"), (dn, "DOWN"), (fl, "FLAT"))[1]
        conf = "MODERATE/HIGH" if len(votes) >= 3 and max(up, dn) >= 0.60 else "LOW"
        print(f"     SIGNAL: {call}   Expected: {pct:+.2f}%   (UP: {up:.0%} | DOWN: {dn:.0%} | FLAT: {fl:.0%})   Confidence: {conf}")
        for v, i, st, w in sorted(votes, key=lambda x: -x[3])[:3]:
            print(f"     * [{st['score']:.2f} score | {st['repeat_sources']} src] {idx.rows[i]['title']}")
            for _, s, j in v["neigh"][:2]:
                o = outcomes[(j, h)]
                print(f"         └─ {day(idx.rows[j]['ts'])} {o[1]:<4} {o[0]:+.2f}% (sim {s:.2f}) {idx.rows[j]['title'][:60]}")


def get_market_prediction(db, cfg, symbol="BTCIRT", hours=72):
    """Structured prediction for a single market for UI candle chart forecasting."""
    mkts = cfg.get("markets", {})
    m_info = None
    m_key = "btc"
    for k, v in mkts.items():
        if v.get("symbol") == symbol or k == symbol or v.get("alt_symbol") == symbol:
            m_info = v
            m_key = k
            break
    if not m_info:
        for k, v in mkts.items():
            m_info = v
            m_key = k
            break

    sym = m_info["symbol"] if m_info else (symbol or "BTCIRT")
    name = m_info.get("name", sym) if m_info else sym
    all_articles = all_rows(db, cfg, relevant_only=True)
    market_rows = [r for r in all_articles if not r["market_tag"] or m_key in r["market_tag"].split("|") or r["market_tag"] == "macro"]
    now = time.time()
    recent = [r["id"] for r in market_rows if r["ts"] >= now - hours * 3600]
    outcomes = load_outcomes(db, symbol=sym)

    res = {
        "symbol": sym,
        "market": m_key,
        "name": name,
        "stories": len(recent),
        "history_count": len({a for (a, h) in outcomes}) if outcomes else 0,
        "horizons": {}
    }
    if not recent or not outcomes:
        return res

    p = cfg["predict"]
    idx = Index(market_rows, cfg)
    groups = story_groups(idx, recent, cfg)

    for h in cfg["horizons_hours"]:
        pool = [o for (a, hh), o in outcomes.items() if hh == h]
        if not pool:
            continue
        votes = []
        for g in groups:
            st = story_info(idx, g, cfg)
            c = [(vote(idx, i, h, outcomes, cfg, now), i) for i in g]
            c = [(v, i) for v, i in c if v]
            if c:
                v, i = max(c, key=lambda x: x[0]["weight"])
                votes.append((v, i, st, v["weight"] * (st["score"] if p["use_scores"] else 1.0)))
        if votes:
            W = sum(x[3] for x in votes)
            pct = sum(x[3] * x[0]["pct"] for x in votes) / W
            up = sum(x[3] * x[0]["up"] for x in votes) / W
            dn = sum(x[3] * x[0]["down"] for x in votes) / W
            fl = max(0.0, 1 - up - dn)
            call = max((up, "UP"), (dn, "DOWN"), (fl, "FLAT"))[1]
            conf = "MODERATE" if len(votes) >= 3 and max(up, dn) >= 0.60 else "LOW"
            drivers = []
            for v, i, st, w in sorted(votes, key=lambda x: -x[3])[:4]:
                r = idx.rows[i]
                hist_matches = []
                for _, s, j in v["neigh"][:2]:
                    o = outcomes.get((j, h))
                    if o:
                        hist_matches.append({
                            "date": day(idx.rows[j]["ts"]),
                            "direction": o[1],
                            "pct": round(o[0], 2),
                            "similarity": round(s, 2),
                            "title": idx.rows[j]["title"]
                        })
                call_i = "UP" if v["up"] > v["down"] else ("DOWN" if v["down"] > v["up"] else "FLAT")
                drivers.append({
                    "id": r["id"],
                    "title": r["title"],
                    "publisher": r["publisher"],
                    "url": r["url"],
                    "score": round(st["score"], 2),
                    "published": r["published_utc"],
                    "event_clue": r["event_clue"] if "event_clue" in r.keys() and r["event_clue"] else "",
                    "event_lag_hours": round(r["event_lag_hours"], 1) if "event_lag_hours" in r.keys() and r["event_lag_hours"] is not None else None,
                    "direction": call_i,
                    "pct": round(v["pct"], 2),
                    "weight": round(w, 2),
                    "matches": hist_matches
                })

            res["horizons"][str(h)] = {
                "horizon": h,
                "pct": round(pct, 2),
                "direction": call,
                "confidence": conf,
                "up": round(up * 100, 1),
                "down": round(dn * 100, 1),
                "flat": round(fl * 100, 1),
                "votes": len(votes),
                "drivers": drivers
            }

    seen_ids = set()
    key_drivers = []
    for h_str, h_data in res["horizons"].items():
        for d in h_data.get("drivers", []):
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                key_drivers.append(d)
    res["key_drivers"] = key_drivers
    return res


def predict(db, cfg, hours, market="all"):
    warn_fake(cfg)
    mkts = cfg.get("markets", {})
    rows = all_rows(db, cfg, relevant_only=True)
    if not rows:
        print("No relevant articles found. Run `collect` first.")
        return

    target_markets = [(k, v) for k, v in mkts.items() if market in ("all", None, k)]
    if not target_markets:
        sym = cfg.get("market", {}).get("symbol", "BTCIRT")
        target_markets = [("default", {"name": "Market", "symbol": sym})]

    for m_key, m_info in target_markets:
        _predict_single_market(db, cfg, hours, m_key, m_info, rows)
    print("\nTip: run `python News.py backtest` to verify whether predictions outperform baseline guessing.")


def backtest(db, cfg, horizon=None, market="all"):
    warn_fake(cfg)
    mkts = cfg.get("markets", {})
    all_articles = all_rows(db, cfg, relevant_only=True)
    if not all_articles:
        print("No relevant articles found.")
        return

    target_markets = [(k, v) for k, v in mkts.items() if market in ("all", None, k)]
    if not target_markets:
        sym = cfg.get("market", {}).get("symbol", "BTCIRT")
        target_markets = [("default", {"name": "Market", "symbol": sym})]

    for m_key, m_info in target_markets:
        sym = m_info["symbol"]
        name = m_info.get("name", sym)
        rows = [r for r in all_articles if not r["market_tag"] or m_key in r["market_tag"].split("|") or r["market_tag"] == "macro"]
        outcomes = load_outcomes(db, symbol=sym)
        if not outcomes:
            continue
        idx = Index(rows, cfg)
        print(f"\n=== Backtest: {name} ({sym}) [{len(rows)} articles, {len(outcomes)} labeled outcomes] ===")
        for h in [horizon] if horizon else cfg["horizons_hours"]:
            n = hits = strong_n = strong_hits = skipped = kw_n = kw_hits = 0
            actual_count = Counter()
            for r in rows:
                o = outcomes.get((r["id"], h))
                if not o or o[1] == "flat":
                    continue
                if r["direction"] in ("up", "down"):
                    kw_n += 1
                    kw_hits += r["direction"] == o[1]
                v = vote(idx, r["id"], h, outcomes, cfg, r["ts"])
                if not v or v["up"] == v["down"]:
                    skipped += 1
                    continue
                pred = "up" if v["up"] > v["down"] else "down"
                n += 1
                actual_count[o[1]] += 1
                hits += pred == o[1]
                if abs(v["up"] - v["down"]) >= 0.4:
                    strong_n += 1
                    strong_hits += pred == o[1]
            if not n:
                print(f"  +{h}h: not enough samples.")
                continue
            base = max(actual_count.values()) / n
            print(f"  +{h}h: accuracy {hits/n:.1%} on {n} calls (majority base: {base:.1%}); skipped {skipped}", end="")
            if strong_n:
                print(f"; strong-signal: {strong_hits/strong_n:.1%} ({strong_n} calls)", end="")
            if kw_n:
                print(f"; headline-sentiment: {kw_hits/kw_n:.1%}", end="")
            print()


def get_all_market_predictions(db, cfg, hours=72, market="all"):
    """Compute structured predictions for all target markets."""
    mkts = cfg.get("markets", {})
    target = [(k, v) for k, v in mkts.items() if market in ("all", None, k)]
    if not target:
        sym = cfg.get("market", {}).get("symbol", "BTCIRT")
        target = [("default", {"name": "Market", "symbol": sym})]
    res = []
    for m_key, m_info in target:
        sym = m_info["symbol"]
        p = get_market_prediction(db, cfg, symbol=sym, hours=hours)
        res.append(p)
    return res


def get_backtest_stats(db, cfg, market="all", horizon=None):
    """Compute structured backtest win-rates and edge metrics for all target markets."""
    mkts = cfg.get("markets", {})
    all_articles = all_rows(db, cfg, relevant_only=True)
    if not all_articles:
        return []

    target_markets = [(k, v) for k, v in mkts.items() if market in ("all", None, k)]
    if not target_markets:
        sym = cfg.get("market", {}).get("symbol", "BTCIRT")
        target_markets = [("default", {"name": "Market", "symbol": sym})]

    results = []
    for m_key, m_info in target_markets:
        sym = m_info["symbol"]
        name = m_info.get("name", sym)
        rows = [r for r in all_articles if not r["market_tag"] or m_key in r["market_tag"].split("|") or r["market_tag"] == "macro"]
        outcomes = load_outcomes(db, symbol=sym)
        if not outcomes:
            continue
        idx = Index(rows, cfg)
        mkt_res = {"market": m_key, "name": name, "symbol": sym, "articles": len(rows), "outcomes": len(outcomes), "horizons": {}}
        for h in [horizon] if horizon else cfg["horizons_hours"]:
            n = hits = strong_n = strong_hits = skipped = kw_n = kw_hits = 0
            actual_count = Counter()
            for r in rows:
                o = outcomes.get((r["id"], h))
                if not o or o[1] == "flat":
                    continue
                if r["direction"] in ("up", "down"):
                    kw_n += 1
                    kw_hits += r["direction"] == o[1]
                v = vote(idx, r["id"], h, outcomes, cfg, r["ts"])
                if not v or v["up"] == v["down"]:
                    skipped += 1
                    continue
                pred = "up" if v["up"] > v["down"] else "down"
                n += 1
                actual_count[o[1]] += 1
                hits += pred == o[1]
                if abs(v["up"] - v["down"]) >= 0.4:
                    strong_n += 1
                    strong_hits += pred == o[1]
            if n:
                base = max(actual_count.values()) / n
                acc = hits / n
                mkt_res["horizons"][str(h)] = {
                    "horizon": h,
                    "calls": n,
                    "accuracy": round(acc * 100, 1),
                    "base_rate": round(base * 100, 1),
                    "edge": round((acc - base) * 100, 1),
                    "skipped": skipped,
                    "strong_calls": strong_n,
                    "strong_accuracy": round((strong_hits / strong_n) * 100, 1) if strong_n else None,
                    "sentiment_calls": kw_n,
                    "sentiment_accuracy": round((kw_hits / kw_n) * 100, 1) if kw_n else None,
                }
        results.append(mkt_res)
    return results


# ---------------------------------------------------------------------- report

def top(db, cfg, hours, n, show_merges=False):
    now = time.time()
    rows = [r for r in all_rows(db, cfg) if r["ts"] >= now - hours * 3600]
    if not rows:
        print(f"No articles in the last {hours}h.")
        return
    stories, notes = annotate(rows, cfg)
    for st in sorted(stories, key=lambda s: -s["score"])[:n]:
        r = st["best"]
        tag = f" x{st['repeat']}" if st["repeat"] > 1 else ""
        ev = f" [🕒 {r['event_clue']} -{r['event_lag_hours']}h]" if ('event_clue' in r.keys() and r['event_clue']) else ""
        print(f"{st['score']:.2f}{tag} [{'yes' if r['relevant'] else 'no':3}|{r['direction'] or '-':5}]{ev} "
              f"{r['publisher']}: {r['title']}")
    if show_merges and notes:
        print("\n--- merges ---")
        print("\n".join(notes))


def export(db, cfg, show_merges=False):
    hs = cfg["horizons_hours"]
    outcomes = load_outcomes(db)
    price0 = dict(db.execute("SELECT article_id, price0 FROM outcomes"))
    stories, notes = annotate(all_rows(db, cfg), cfg)
    cols = (["score", "base_score", "relevant", "repeat_type", "publisher", "title", "url",
             "relevancy", "source_score", "buzz", "matched", "direction", "repeat",
             "repeat_sources", "also_reported_by", "series_editions", "published_utc",
             "event_utc", "event_clue", "event_lag_hours",
             "body_status", "price_at_news"]
            + [f"{k}_{h}h" for h in hs for k in ("pct", "dir")] + ["summary", "body"])
    os.makedirs(os.path.dirname(EXPORT_FILE), exist_ok=True)
    with open(EXPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for st in sorted(stories, key=lambda s: -s["score"]):
            r = st["best"]
            ev_utc = r["event_utc"] if "event_utc" in r.keys() else ""
            ev_clue = r["event_clue"] if "event_clue" in r.keys() else ""
            ev_lag = r["event_lag_hours"] if "event_lag_hours" in r.keys() else ""
            row = [st["score"], r["score"], "yes" if r["relevant"] else "no", st["repeat_type"],
                   r["publisher"], r["title"], r["url"], r["relevancy"], r["source_score"],
                   r["buzz"], r["matched"], r["direction"], st["repeat"], st["repeat_sources"],
                   st["also"], st["editions"], r["published_utc"],
                   ev_utc, ev_clue, ev_lag,
                   r["body_status"], price0.get(r["id"], "")]
            for h in hs:
                o = outcomes.get((r["id"], h))
                row += [round(o[0], 3), o[1]] if o else ["", ""]
            w.writerow(row + [r["summary"], r["body"]])
    print(f"exported {len(stories)} stories to {EXPORT_FILE}")
    if show_merges and notes:
        print("\n--- merges ---")
        print("\n".join(notes))


# ---------------------------------------------------------------------- doctor

def doctor(db, cfg):
    """Diagnose data counts, multi-market configuration, and pipeline status."""
    mkts = cfg.get("markets", {})
    art_count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    rel_count = db.execute("SELECT COUNT(*) FROM articles WHERE relevant=1").fetchone()[0]
    body_count = db.execute("SELECT COUNT(*) FROM articles WHERE body_status='ok'").fetchone()[0]
    total_out = db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    ev_count = db.execute("SELECT COUNT(*) FROM articles WHERE event_ts IS NOT NULL").fetchone()[0]
    avg_lag = db.execute("SELECT AVG(event_lag_hours) FROM articles WHERE event_lag_hours IS NOT NULL").fetchone()[0] or 0.0

    print(f"=== Database Diagnostic ({DB_FILE}) ===")
    print(f"Articles:          {art_count} total ({rel_count} relevant, {body_count} with bodies)")
    print(f"Event Time Clues:  {ev_count} in-text timestamps extracted (avg lag: {avg_lag:.1f}h)")
    
    tags = db.execute("SELECT market_tag, COUNT(*) FROM articles WHERE relevant=1 GROUP BY market_tag").fetchall()
    print("Market breakdown: ", ", ".join(f"{r[0] or 'macro'}: {r[1]}" for r in tags))
    
    print("\n--- Active Markets ---")
    healthy = 0
    for m_key, m_info in mkts.items():
        sym = m_info["symbol"]
        res = m_info.get("resolution", "15")
        can = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE symbol=?", (sym,)).fetchone()
        out = db.execute("SELECT COUNT(*) FROM outcomes WHERE symbol=?", (sym,)).fetchone()[0]
        date_range = f"{day(can[1])} .. {day(can[2])}" if can[1] else "no candles"
        print(f"  [{m_key.upper()}] {m_info['name']} ({sym}):")
        print(f"        Candles: {can[0]} ({date_range}, res {res})")
        print(f"        Outcomes: {out} labeled")
        if can[0] > 0 and out > 0:
            healthy += 1

    print("\n--- Pipeline Status ---")
    if healthy == len(mkts):
        print(f"[✓] Pipeline healthy! All {len(mkts)} markets loaded and ready for `predict`, `backtest`, and `top`.")
    else:
        print(f"[*] {healthy}/{len(mkts)} markets labeled. Run `python News.py label` to update outcomes.")
