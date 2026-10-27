"""Using the data: similarity, prediction, backtest, top list, CSV export."""
import csv
import math
import os
import time
from collections import Counter, defaultdict

from common import EXPORT_FILE, all_rows, day, load_outcomes, tokens
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


def predict(db, cfg, hours):
    warn_fake(cfg)
    p = cfg["predict"]
    rows = all_rows(db, cfg, relevant_only=True)
    now = time.time()
    recent = [r["id"] for r in rows if r["ts"] >= now - hours * 3600]
    if not recent:
        print(f"No relevant articles in the last {hours}h. Run `collect` first.")
        return
    idx, outcomes = Index(rows, cfg), load_outcomes(db)
    groups = story_groups(idx, recent, cfg)
    print(f"{len(recent)} relevant articles in the last {hours}h = {len(groups)} distinct stories\n"
          f"market: {cfg['market']['symbol']}")
    for h in cfg["horizons_hours"]:
        pool = [o for (a, hh), o in outcomes.items() if hh == h]
        if not pool:
            print(f"\n== +{h}h: no labeled history yet (collect older dates, then `label`)")
            continue
        n = len(pool)
        print(f"\n== +{h}h   history base rate: {sum(o[1]=='up' for o in pool)/n:.0%} up / "
              f"{sum(o[1]=='down' for o in pool)/n:.0%} down ({n} labeled articles)")
        votes = []
        for g in groups:
            st = story_info(idx, g, cfg)
            c = [(vote(idx, i, h, outcomes, cfg, now), i) for i in g]
            c = [(v, i) for v, i in c if v]
            if c:
                v, i = max(c, key=lambda x: x[0]["weight"])
                votes.append((v, i, st, v["weight"] * (st["score"] if p["use_scores"] else 1.0)))
        if not votes:
            print("   no story has enough similar history -> no prediction")
            continue
        W = sum(x[3] for x in votes)
        pct = sum(x[3] * x[0]["pct"] for x in votes) / W
        up = sum(x[3] * x[0]["up"] for x in votes) / W
        dn = sum(x[3] * x[0]["down"] for x in votes) / W
        fl = max(0.0, 1 - up - dn)
        call = max((up, "UP"), (dn, "DOWN"), (fl, "FLAT"))[1]
        conf = "moderate" if len(votes) >= 3 and max(up, dn) >= 0.65 else "low"
        print(f"   PREDICTION: {call}   expected {pct:+.2f}%   (up {up:.0%} / down {dn:.0%} / "
              f"flat {fl:.0%})   stories with evidence: {len(votes)}/{len(groups)}   confidence: {conf}")
        for v, i, st, w in sorted(votes, key=lambda x: -x[3])[:4]:
            print(f"   - [score {st['score']:.2f} | {st['repeat_sources']} outlet(s)] {idx.rows[i]['title']}")
            for _, s, j in v["neigh"][:2]:
                o = outcomes[(j, h)]
                print(f"       {day(idx.rows[j]['ts'])}  {o[1]:<4} {o[0]:+.2f}%  sim {s:.2f}  "
                      f"score {idx.rows[j]['score']:.2f}  {idx.rows[j]['title'][:70]}")
    print("\nTip: run `python News.py backtest` to see whether this beats the base rate.")


def backtest(db, cfg, horizon=None):
    warn_fake(cfg)
    rows = all_rows(db, cfg, relevant_only=True)
    idx, outcomes = Index(rows, cfg), load_outcomes(db)
    for h in [horizon] if horizon else cfg["horizons_hours"]:
        n = hits = strong_n = strong_hits = skipped = kw_n = kw_hits = 0
        actual_count = Counter()
        for r in rows:
            o = outcomes.get((r["id"], h))
            if not o or o[1] == "flat":
                continue
            if r["direction"] in ("up", "down"):  # naive baseline: trust the headline's own words
                kw_n += 1
                kw_hits += r["direction"] == o[1]
            v = vote(idx, r["id"], h, outcomes, cfg, r["ts"])  # only history known at that moment
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
            print(f"+{h}h: not enough labeled history to test.")
            continue
        base = max(actual_count.values()) / n
        print(f"+{h}h: accuracy {hits/n:.1%} on {n} calls (always-guess-majority: {base:.1%}); "
              f"no-vote {skipped}", end="")
        print(f"; strong-signal calls {strong_hits/strong_n:.1%} on {strong_n}" if strong_n else "", end="")
        print(f"; headline-words-only {kw_hits/kw_n:.1%} on {kw_n}" if kw_n else "")
    print("Only trust the predictor if it clearly beats the majority guess on a decent sample.")


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
        print(f"{st['score']:.2f}{tag} [{'yes' if r['relevant'] else 'no':3}|{r['direction'] or '-':5}] "
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
             "body_status", "price_at_news"]
            + [f"{k}_{h}h" for h in hs for k in ("pct", "dir")] + ["summary", "body"])
    os.makedirs(os.path.dirname(EXPORT_FILE), exist_ok=True)
    with open(EXPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for st in sorted(stories, key=lambda s: -s["score"]):
            r = st["best"]
            row = [st["score"], r["score"], "yes" if r["relevant"] else "no", st["repeat_type"],
                   r["publisher"], r["title"], r["url"], r["relevancy"], r["source_score"],
                   r["buzz"], r["matched"], r["direction"], st["repeat"], st["repeat_sources"],
                   st["also"], st["editions"], r["published_utc"], r["body_status"],
                   price0.get(r["id"], "")]
            for h in hs:
                o = outcomes.get((r["id"], h))
                row += [round(o[0], 3), o[1]] if o else ["", ""]
            w.writerow(row + [r["summary"], r["body"]])
    print(f"exported {len(stories)} stories to {EXPORT_FILE}")
    if show_merges and notes:
        print("\n--- merges ---")
        print("\n".join(notes))

