"""Local web UI:  python News.py ui [--port 8765]   (standard library only, localhost only)."""
import contextlib
import io
import json
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from analysis import (annotate, backtest, doctor, export,
                      get_all_market_predictions, get_backtest_stats,
                      get_market_prediction, predict, top)
from common import all_rows, load_config, load_outcomes, open_db, rescore
from data import collect, fetch_live_rates, label, scrape_bodies

LOCK = threading.Lock()


def _date(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def _dispatch(name, db, cfg, a):
    if name == "collect":
        d0, d1 = _date(a.get("start")), _date(a.get("end"))
        if d0 and not d1:
            d1 = d0
        collect(db, cfg, d0, d1)
    elif name == "bodies":
        scrape_bodies(db, cfg, int(a.get("limit", 40)))
    elif name == "label":
        label(db, cfg, market_filter=a.get("market", "all"))
    elif name == "rescore":
        rescore(db, cfg)
    elif name == "export":
        export(db, cfg)
    elif name == "predict":
        predict(db, cfg, float(a.get("hours", 24)), market=a.get("market", "all"))
    elif name == "backtest":
        backtest(db, cfg, int(a["horizon"]) if a.get("horizon") else None, market=a.get("market", "all"))
    elif name == "doctor":
        doctor(db, cfg)
    elif name in ("update", "sync_all"):
        print("=== [1/4] Collecting Latest Feeds ===")
        collect(db, cfg)
        if a.get("include_bodies", True):
            print("\n=== [2/4] Downloading Article Bodies ===")
            scrape_bodies(db, cfg, int(a.get("limit", 40)))
        print("\n=== [3/4] Syncing Prices & Labeling Outcomes (Dollar, Gold, BTC) ===")
        label(db, cfg, market_filter="all")
        if a.get("run_predictions", True):
            print("\n=== [4/4] Multi-Market Predictions ===")
            predict(db, cfg, 72, market="all")
        print("\n[✓] Full Sync Complete! All 3 markets are up to date.")
    else:
        print(f"unknown command: {name}")


def run_job(name, args):
    if not LOCK.acquire(blocking=False):
        return "Another job is still running. Wait for it to finish."
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cfg, db = load_config(), open_db()
                if db.execute("SELECT 1 FROM articles WHERE score IS NULL LIMIT 1").fetchone():
                    rescore(db, cfg)
                _dispatch(name, db, cfg, args)
            except SystemExit as e:
                print(e.code if isinstance(e.code, str) else f"stopped (exit {e.code})")
            except Exception:
                traceback.print_exc(file=buf)
    finally:
        LOCK.release()
    return buf.getvalue().strip() or "(done, nothing to report)"


def api_stats():
    cfg, db = load_config(), open_db()
    one = lambda q, *p: db.execute(q, p).fetchone()[0]
    mkts = cfg.get("markets", {})
    mkt_info = {}
    for k, v in mkts.items():
        sym = v["symbol"]
        c_cnt = one("SELECT COUNT(*) FROM candles WHERE symbol=?", sym)
        o_cnt = one("SELECT COUNT(*) FROM outcomes WHERE symbol=?", sym)
        mkt_info[k] = {"name": v["name"], "symbol": sym, "candles": c_cnt, "outcomes": o_cnt}
    return {
        "articles": one("SELECT COUNT(*) FROM articles"),
        "relevant": one("SELECT COUNT(*) FROM articles WHERE relevant=1"),
        "bodies": one("SELECT COUNT(*) FROM articles WHERE body_status='ok'"),
        "events": one("SELECT COUNT(*) FROM articles WHERE event_ts IS NOT NULL"),
        "outcomes": one("SELECT COUNT(*) FROM outcomes"),
        "markets": mkt_info,
        "horizons": cfg["horizons_hours"],
    }


def api_ticker():
    return fetch_live_rates()


def api_prediction(q):
    sym = q.get("symbol", ["BTCIRT"])[0]
    hours = float(q.get("hours", ["72"])[0])
    cfg, db = load_config(), open_db()
    return get_market_prediction(db, cfg, sym, hours)


def api_predict_view(q):
    hours = float(q.get("hours", ["72"])[0])
    mkt = q.get("market", ["all"])[0]
    cfg, db = load_config(), open_db()
    return get_all_market_predictions(db, cfg, hours=hours, market=mkt)


def api_backtest_view(q):
    mkt = q.get("market", ["all"])[0]
    h = int(q.get("horizon", [0])[0]) or None
    cfg, db = load_config(), open_db()
    return get_backtest_stats(db, cfg, market=mkt, horizon=h)


def api_database(q):
    cfg, db = load_config(), open_db()
    page = max(1, int(q.get("page", ["1"])[0]))
    page_size = min(200, max(10, int(q.get("page_size", ["25"])[0])))
    search_q = q.get("q", [""])[0].strip()
    mkt = q.get("market", ["all"])[0]
    rel_only = q.get("relevant", ["0"])[0] in ("1", "true")
    event_only = q.get("event_only", ["0"])[0] in ("1", "true")
    has_body = q.get("has_body", ["0"])[0] in ("1", "true")
    sort_col = q.get("sort_col", ["ts"])[0]
    sort_dir = q.get("sort_dir", ["desc"])[0]

    where = []
    params = []

    if search_q:
        where.append("(title LIKE ? OR summary LIKE ? OR publisher LIKE ?)")
        term = f"%{search_q}%"
        params.extend([term, term, term])

    if mkt != "all":
        where.append("(market_tag LIKE ? OR market_tag='macro' OR market_tag='')")
        params.append(f"%{mkt}%")

    if rel_only:
        where.append("relevant=1")

    if event_only:
        where.append("event_ts IS NOT NULL")

    if has_body:
        where.append("body_status='ok'")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) FROM articles {where_sql}", params).fetchone()[0]

    valid_cols = {"id", "published_utc", "ts", "score", "relevancy", "source_score", "buzz", "publisher", "title", "market_tag", "event_lag_hours"}
    col = sort_col if sort_col in valid_cols else "ts"
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    offset = (page - 1) * page_size
    query = f"""
        SELECT id, url, publisher, title, summary, published_utc, ts, relevant, score, relevancy, source_score, buzz,
               direction, market_tag, event_ts, event_utc, event_clue, event_lag_hours, body_status, length(body) as body_len
        FROM articles
        {where_sql}
        ORDER BY {col} {direction} NULLS LAST
        LIMIT ? OFFSET ?
    """
    rows = db.execute(query, params + [page_size, offset]).fetchall()

    art_ids = [r["id"] for r in rows]
    outcomes = {}
    if art_ids:
        placeholders = ",".join("?" * len(art_ids))
        out_rows = db.execute(
            f"SELECT article_id, symbol, horizon, price0, price1, pct, direction FROM outcomes WHERE article_id IN ({placeholders})",
            art_ids
        ).fetchall()
        for o in out_rows:
            outcomes.setdefault(o["article_id"], {})[str(o["horizon"])] = {
                "symbol": o["symbol"], "pct": round(o["pct"], 2), "dir": o["direction"], "p0": o["price0"]
            }

    data = []
    for r in rows:
        data.append({
            "id": r["id"],
            "url": r["url"],
            "publisher": r["publisher"],
            "title": r["title"],
            "summary": (r["summary"] or "")[:250],
            "published": r["published_utc"],
            "ts": r["ts"],
            "relevant": bool(r["relevant"]),
            "score": round(r["score"], 3) if r["score"] is not None else 0.0,
            "relevancy": round(r["relevancy"], 3) if r["relevancy"] is not None else 0.0,
            "source_score": round(r["source_score"], 3) if r["source_score"] is not None else 0.0,
            "buzz": round(r["buzz"], 3) if r["buzz"] is not None else 0.0,
            "direction": r["direction"] or "",
            "market_tag": r["market_tag"] or "macro",
            "event_utc": r["event_utc"],
            "event_clue": r["event_clue"],
            "event_lag_hours": round(r["event_lag_hours"], 1) if r["event_lag_hours"] is not None else None,
            "body_status": r["body_status"] or ("has_body" if r["body_len"] else "no_body"),
            "outcomes": outcomes.get(r["id"], {})
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 1,
        "rows": data
    }


def api_top(q):
    hours = float(q.get("hours", ["24"])[0])
    n = int(q.get("n", ["100"])[0])
    mkt = q.get("market", ["all"])[0]
    cfg, db = load_config(), open_db()
    now = time.time()
    rows = [r for r in all_rows(db, cfg) if hours <= 0 or r["ts"] >= now - hours * 3600]
    if mkt != "all":
        rows = [r for r in rows if r["market_tag"] and (mkt in r["market_tag"].split("|") or r["market_tag"] == "macro")]
    if not rows:
        return []
    stories, _ = annotate(rows, cfg)
    outs = load_outcomes(db)
    res = []
    for st in sorted(stories, key=lambda s: -s["score"])[:n]:
        r = st["best"]
        res.append({
            "score": st["score"],
            "repeat": st["repeat"],
            "sources": st["repeat_sources"],
            "relevant": bool(r["relevant"]),
            "direction": r["direction"] or "",
            "market_tag": r["market_tag"] or "macro",
            "publisher": r["publisher"],
            "title": r["title"],
            "url": r["url"],
            "published": r["published_utc"],
            "event_utc": r["event_utc"] if "event_utc" in r.keys() and r["event_utc"] else "",
            "event_clue": r["event_clue"] if "event_clue" in r.keys() and r["event_clue"] else "",
            "event_lag_hours": round(r["event_lag_hours"], 1) if "event_lag_hours" in r.keys() and r["event_lag_hours"] is not None else None,
            "also": st["also"],
            "out": {
                str(h): {"pct": round(outs[(r["id"], h)][0], 2), "dir": outs[(r["id"], h)][1]}
                for h in cfg["horizons_hours"]
                if (r["id"], h) in outs
            },
        })
    return res


def api_candles(q):
    hours = float(q.get("hours", ["72"])[0])
    sym = q.get("symbol", ["BTCIRT"])[0]
    cfg, db = load_config(), open_db()
    now = time.time()
    t_min = (now - hours * 3600) if hours > 0 else 0
    rows = db.execute(
        "SELECT ts, o, c FROM candles WHERE symbol=? AND ts >= ? ORDER BY ts ASC",
        (sym, int(t_min)),
    ).fetchall()

    # Determine market key and resolution
    mkts = cfg.get("markets", {})
    m_key = "btc"
    res_val = 900
    for k, v in mkts.items():
        if v.get("symbol") == sym or v.get("daily_symbol") == sym:
            m_key = k
            res_str = v.get("resolution", "15")
            res_val = 86400 if res_str.upper().endswith("D") else int(res_str) * 60
            break

    # Find relevant news in this time window
    news_rows = db.execute(
        "SELECT id, title, publisher, url, published_utc, ts, event_ts, event_clue, event_lag_hours, score, direction "
        "FROM articles WHERE relevant=1 AND ts >= ? AND "
        "(market_tag LIKE '%' || ? || '%' OR market_tag='macro' OR market_tag='') "
        "ORDER BY ts ASC",
        (int(t_min - res_val), m_key)
    ).fetchall()

    candle_news = {}
    for nr in news_rows:
        eff_ts = nr["event_ts"] if nr["event_ts"] else nr["ts"]
        if not eff_ts:
            continue
        c_ts = eff_ts - (eff_ts % res_val)
        candle_news.setdefault(c_ts, []).append({
            "id": nr["id"],
            "title": nr["title"],
            "publisher": nr["publisher"],
            "url": nr["url"],
            "published": nr["published_utc"],
            "event_clue": nr["event_clue"] if nr["event_clue"] else "",
            "event_lag_hours": round(nr["event_lag_hours"], 1) if nr["event_lag_hours"] is not None else None,
            "direction": nr["direction"] or "",
            "score": round(nr["score"] or 0.0, 2)
        })

    out = []
    for r in rows:
        c_ts = r["ts"]
        n_items = candle_news.get(c_ts, [])
        if not n_items:
            for k_ts, items in candle_news.items():
                if abs(k_ts - c_ts) < res_val:
                    n_items = items
                    break
        out.append({
            "ts": r["ts"],
            "open": r["o"],
            "close": r["c"],
            "high": max(r["o"], r["c"]),
            "low": min(r["o"], r["c"]),
            "news": n_items
        })
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(api_stats()))
            if u.path == "/api/ticker":
                return self._send(200, json.dumps(api_ticker()))
            if u.path == "/api/prediction":
                return self._send(200, json.dumps(api_prediction(parse_qs(u.query))))
            if u.path == "/api/top":
                return self._send(200, json.dumps(api_top(parse_qs(u.query))))
            if u.path == "/api/candles":
                return self._send(200, json.dumps(api_candles(parse_qs(u.query))))
            if u.path == "/api/predict_view":
                return self._send(200, json.dumps(api_predict_view(parse_qs(u.query))))
            if u.path == "/api/backtest_view":
                return self._send(200, json.dumps(api_backtest_view(parse_qs(u.query))))
            if u.path == "/api/database":
                return self._send(200, json.dumps(api_database(parse_qs(u.query))))
            self._send(404, "not found", "text/plain")
        except SystemExit as e:
            self._send(500, str(e.code), "text/plain")
        except Exception as e:
            self._send(500, f"{type(e).__name__}: {e}", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-UI") != "1":
            return self._send(403, "forbidden", "text/plain")
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            out = run_job(str(data.get("cmd", "")), data.get("args") or {})
            self._send(200, json.dumps({"output": out}))
        except Exception as e:
            self._send(500, f"{type(e).__name__}: {e}", "text/plain")


def serve(port=8765, open_browser=True):
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(
            f"Cannot start the UI on port {port}: {e}\n"
            f"Is it already running? Try another port:  python News.py ui --port {port + 1}"
        )
    url = f"http://127.0.0.1:{port}/"
    print(f"News UI running at {url}", flush=True)
    print("Open that address in your browser if it does not open by itself. Ctrl+C stops it.", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News → Price Prediction Tracker</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1b19;--mut:#6b6a66;--card:#fff;--line:#e4e2dc;--acc:#2b5fd9;
--up:#13734a;--upbg:#e0f3e9;--dn:#a3262a;--dnbg:#fbe4e4;--fl:#555;--flbg:#ebebe8;--pred:#7c3aed;--predbg:#f3e8ff}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--mut:#9a988f;--card:#1f1f1c;
--line:#33332f;--acc:#7ea2ff;--up:#6fd9a3;--upbg:#173a2a;--dn:#ff9a9d;--dnbg:#43201f;--fl:#bbb;--flbg:#2c2c29;--pred:#c084fc;--predbg:#2e1065}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
header{padding:16px 20px 0;max-width:1150px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}
#stats{color:var(--mut);font-size:13px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);max-width:1150px;margin:12px auto 0;padding:0 20px;align-items:center}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;color:var(--mut);
font:inherit;cursor:pointer}
nav button.on{color:var(--fg);border-color:var(--acc);font-weight:600}
main{max-width:1150px;margin:0 auto;padding:16px 20px 60px}
.tab{display:none}.tab.on{display:block}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
select,input,button.act{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}
button.act{cursor:pointer}button.act:hover{border-color:var(--acc)}
button.act.main{background:var(--acc);border-color:var(--acc);color:#fff}
button.act.pred-on{background:var(--pred);border-color:var(--pred);color:#fff}
button:disabled{opacity:.5;cursor:wait}
.story{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin-bottom:8px}
.story .t{font-weight:600;color:var(--fg);text-decoration:none}.story .t:hover{color:var(--acc)}
.meta{color:var(--mut);font-size:13px;margin-top:3px;display:flex;flex-wrap:wrap;gap:4px 12px}
.score{display:inline-block;min-width:42px;text-align:center;font-weight:700;border-radius:5px;
padding:1px 6px;background:var(--flbg);margin-right:8px;font-variant-numeric:tabular-nums}
.badge{font-size:12px;border-radius:4px;padding:1px 6px;background:var(--flbg);color:var(--fl)}
.badge.up{background:var(--upbg);color:var(--up)}.badge.down{background:var(--dnbg);color:var(--dn)}
.badge.pred{background:var(--predbg);color:var(--pred);font-weight:600}
.badge.event-time{background:rgba(168,85,247,0.15);color:#c084fc;border:1px solid rgba(168,85,247,0.4);font-weight:600}
.outs{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;
white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,Consolas,monospace;min-height:80px}
.empty{color:var(--mut);padding:30px 0;text-align:center}
.grp{margin-bottom:18px}.grp h3{font-size:14px;margin:0 0 6px}
.hint{color:var(--mut);font-size:13px;margin:4px 0 10px}
.hero-box{background:var(--card);border:2px solid var(--acc);border-radius:10px;padding:16px 20px;margin-bottom:18px}
.step-pill{font-size:12px;padding:3px 8px;border-radius:12px;background:var(--flbg);color:var(--mut)}
.step-pill.active{background:var(--acc);color:#fff;font-weight:600}
.step-pill.done{background:var(--upbg);color:var(--up);font-weight:600}
#tooltip{position:absolute;display:none;background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:8px 12px;font-size:12px;box-shadow:0 6px 16px rgba(0,0,0,0.18);pointer-events:none;z-index:30}
/* Database Excel Table Styling */
.excel-table{width:100%;border-collapse:collapse;font-size:12px;text-align:left;}
.excel-table th{background:var(--card);border-bottom:2px solid var(--line);border-right:1px solid var(--line);padding:7px 8px;font-weight:600;color:var(--fg);cursor:pointer;user-select:none;white-space:nowrap;}
.excel-table th:hover{background:var(--flbg);}
.excel-table td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:5px 8px;vertical-align:middle;}
.excel-table tr:hover td{background:rgba(43,95,217,0.06);}
.excel-table tr:nth-child(even) td{background:rgba(0,0,0,0.015);}
@media(prefers-color-scheme:dark){.excel-table tr:nth-child(even) td{background:rgba(255,255,255,0.015);}}

/* Rank badges */
.rank-badge{display:inline-block;padding:2px 7px;border-radius:12px;font-size:11px;font-weight:700;background:var(--flbg);color:var(--mut);margin-right:8px;}
.rank-badge.top-1{background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff;box-shadow:0 2px 6px rgba(245,158,11,0.3);}
.rank-badge.top-2{background:linear-gradient(135deg,#94a3b8,#64748b);color:#fff;box-shadow:0 2px 6px rgba(148,163,184,0.3);}
.rank-badge.top-3{background:linear-gradient(135deg,#b45309,#78350f);color:#fff;box-shadow:0 2px 6px rgba(180,83,9,0.3);}

/* Predict & Backtest Visual Cards */
.mkt-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px;}
.horizon-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:12px;}
.horizon-card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px 14px;}
.signal-pill{display:inline-block;padding:3px 10px;border-radius:6px;font-weight:700;font-size:13px;}
.signal-pill.UP{background:var(--upbg);color:var(--up);}
.signal-pill.DOWN{background:var(--dnbg);color:var(--dn);}
.signal-pill.FLAT{background:var(--flbg);color:var(--fl);}
.prob-track{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line);margin:8px 0 6px;}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:12px 0;}
.kpi-card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;text-align:center;}
.kpi-val{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.2;}
.kpi-lbl{font-size:10px;color:var(--mut);margin-top:3px;text-transform:uppercase;letter-spacing:0.3px;}
</style></head><body>
<header>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
 <div>
  <h1>Multi-Market News & Price Tracker</h1>
  <div id="stats">loading…</div>
 </div>
 <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
  <div id="live-ticker" style="display:flex;gap:6px;flex-wrap:wrap;font-size:12px;"></div>
  <button class="act main" id="header-quick-sync" style="font-weight:600;padding:5px 12px;font-size:13px;">⚡ Quick Sync</button>
 </div>
</div>
</header>
<nav id="nav">
 <button data-t="stories" class="on">Stories</button>
 <button data-t="market">Market &amp; Prices</button>
 <button data-t="analysis">Predict &amp; Backtest</button>
 <button data-t="database">Database (Excel View)</button>
 <button data-t="data">Sync News &amp; Data</button>
</nav>
<main>
<section id="stories" class="tab on">
 <div class="bar">
  <label>Period <select id="hours">
   <option value="6">6 h</option>
   <option value="24">24 h</option>
   <option value="72" selected>3 days</option>
   <option value="168">7 days</option>
   <option value="0">all</option>
  </select></label>
  <label>Market <select id="mfilter">
   <option value="all" selected>All Markets</option>
   <option value="dollar">💵 US Dollar</option>
   <option value="gold">🥇 Gold &amp; Coin</option>
   <option value="btc">₿ Bitcoin</option>
  </select></label>
  <label>Sort By <select id="story-sort">
   <option value="score_desc" selected>⭐ Score: High to Low</option>
   <option value="score_asc">Score: Low to High</option>
   <option value="date_desc">📅 Date: Newest First</option>
   <option value="date_asc">📅 Date: Oldest First</option>
   <option value="repeats_desc">📢 Most Covered (Outlets)</option>
   <option value="move_desc">💥 Largest Price Move</option>
  </select></label>
  <label>Direction <select id="story-dir">
   <option value="all" selected>All</option>
   <option value="up">🟢 Bullish (UP)</option>
   <option value="down">🔴 Bearish (DOWN)</option>
   <option value="mixed">⚪ Mixed</option>
  </select></label>
  <label><input type="checkbox" id="relonly"> relevant only</label>
  <label><input type="checkbox" id="eventonly"> 🕒 event time only</label>
  <input id="q" placeholder="filter by headline / text…" style="flex:1;min-width:160px">
  <button class="act" id="reload">Refresh</button>
 </div>
 <div id="story-meta-bar" style="display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--mut);margin-bottom:10px;">
  <span id="story-count-info">Loading stories…</span>
  <span id="story-sort-info">Ranked by Score</span>
 </div>
 <div id="list"></div>
</section>

<section id="market" class="tab">
 <div class="bar">
  <label>Asset <select id="msymbol">
   <option value="BTCIRT" selected>₿ Bitcoin (BTCIRT - 15m)</option>
   <option value="USDTIRT">💵 US Dollar (USDTIRT - 15m)</option>
   <option value="PAXGIRT">🥇 Gold PAXG (PAXGIRT - 15m)</option>
   <option value="USD_IRT">💵 US Dollar Free Market (USD_IRT - Daily)</option>
   <option value="GOLD_SEKEE">🥇 Gold Emami Coin (GOLD_SEKEE - Daily)</option>
  </select></label>
  <label>Window <select id="mhours">
   <option value="24">24 h</option>
   <option value="72" selected>3 days</option>
   <option value="168">7 days</option>
   <option value="720">30 days</option>
   <option value="0">All</option>
  </select></label>
  <button class="act" id="btn-toggle-pred" style="border-color:var(--pred);font-weight:600;">🔮 Show Prediction Candles</button>
  <button class="act" id="reload-candles">Refresh Prices</button>
 </div>

 <div id="pred-banner" style="display:none;padding:8px 12px;background:var(--predbg);border:1px solid var(--pred);border-radius:6px;margin-bottom:10px;font-size:13px;"></div>
 <div id="pred-drivers-box" style="display:none;margin-bottom:12px;background:var(--card);border:1px solid var(--pred);border-radius:8px;padding:12px 14px;"></div>

 <div class="story" style="position:relative;padding:10px 14px 14px;">
  <!-- TradingView style top hover bar -->
  <div id="chart-hover-bar" style="display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--mut);font-family:ui-monospace,monospace;border-bottom:1px solid var(--line);padding-bottom:6px;margin-bottom:8px;">
   <div id="ch-title"><span id="ch-sym">BTCIRT</span> · <span id="ch-range">3 days</span></div>
   <div id="ch-hover-info">Hover over any candle for exact price</div>
  </div>

  <div id="tooltip"></div>
  <div style="overflow-x:auto;">
   <svg id="candle-chart" width="1100" height="320" style="display:block;width:100%;cursor:crosshair;"></svg>
  </div>
 </div>

 <div id="candle-selected-news" style="display:none;margin-top:12px;background:var(--card);border:1px solid var(--pred);border-radius:8px;padding:12px 14px;"></div>
 <div id="candle-table-box" style="margin-top:14px;max-height:350px;overflow-y:auto;"></div>
</section>

<section id="analysis" class="tab">
 <div class="bar">
  <label>Target Market <select id="pmarket">
   <option value="all" selected>All 3 Markets</option>
   <option value="dollar">💵 US Dollar</option>
   <option value="gold">🥇 Gold &amp; Coins</option>
   <option value="btc">₿ Bitcoin</option>
  </select></label>
  <label>News Window <select id="phours">
   <option value="6">6 h</option>
   <option value="24">24 h</option>
   <option value="72" selected>3 days</option>
   <option value="168">7 days</option>
  </select></label>
  <button class="act main" id="btn-run-predictions" style="font-weight:600;">🔮 Run Market Predictions</button>
  <button class="act" id="btn-run-backtest" style="font-weight:600;">📊 Run Backtest Validation</button>
  <button class="act" id="btn-toggle-raw" style="font-size:12px;margin-left:auto;">Terminal Log View</button>
 </div>

 <div id="analysis-visual-container">
  <div id="predict-results-view"></div>
  <div id="backtest-results-view" style="display:none;"></div>
 </div>

 <pre id="out-analysis" style="display:none;margin-top:14px;">Press 'Run Market Predictions' or 'Run Backtest Validation' above.</pre>
</section>

<section id="data" class="tab">
 <!-- Simple One-Click Sync Hero Box -->
 <div class="hero-box">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">
   <span style="font-size:24px;">⚡</span>
   <div>
    <h3 style="margin:0;font-size:16px;">One-Click Sync: News + Bodies + Price Labels</h3>
    <div style="color:var(--mut);font-size:13px;margin-top:2px;">
     Runs the complete ingestion pipeline in one click: collects today's news feeds, extracts article bodies, downloads updated candle data for Dollar, Gold &amp; BTC, and labels all outcomes.
    </div>
   </div>
  </div>
  <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding-top:10px;border-top:1px solid var(--line);">
   <button class="act main" id="btn-full-sync" style="font-size:14px;padding:8px 18px;font-weight:700;">
    🚀 Run Full Sync Now
   </button>
   <label style="font-size:13px;display:flex;align-items:center;gap:6px;cursor:pointer;">
    <input type="checkbox" id="sync-scrape-bodies" checked> Download article bodies (up to 40)
   </label>
   <label style="font-size:13px;display:flex;align-items:center;gap:6px;cursor:pointer;">
    <input type="checkbox" id="sync-run-predictions" checked> Refresh predictions
   </label>
  </div>
  <div id="sync-progress" style="display:none;margin-top:14px;">
   <div id="sync-status-msg" style="font-weight:600;font-size:13px;color:var(--acc);margin-bottom:8px;">Running full sync…</div>
   <div style="display:flex;gap:6px;flex-wrap:wrap;">
    <span class="step-pill" id="st-1">1. Collect News</span>
    <span class="step-pill" id="st-2">2. Scrape Bodies</span>
    <span class="step-pill" id="st-3">3. Pull Price Candles</span>
    <span class="step-pill" id="st-4">4. Label Markets</span>
    <span class="step-pill" id="st-5">5. Update Predictions</span>
   </div>
  </div>
 </div>

 <!-- Date Range Backfill -->
 <div class="story" style="margin-bottom:14px;">
  <h3 style="margin:0 0 6px;font-size:14px;">📅 Backfill Historical News by Date Range</h3>
  <div style="color:var(--mut);font-size:13px;margin-bottom:10px;">
   Ingest older dates to grow historical prediction database.
  </div>
  <div class="bar">
   <label>From <input type="date" id="d0"></label>
   <label>To <input type="date" id="d1"></label>
   <button class="act" id="backfill">Collect Date Range</button>
  </div>
 </div>

 <!-- Secondary Tools & Diagnostics -->
 <details class="story" style="cursor:pointer;padding:10px 14px;">
  <summary style="font-weight:600;font-size:14px;">⚙️ Individual Pipeline Tools &amp; Diagnostics</summary>
  <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
   <button class="act" data-run="collect">Collect Today Only</button>
   <button class="act" data-run="bodies">Scrape Bodies Only</button>
   <button class="act" data-run="label">Re-label Prices</button>
   <button class="act" data-run="rescore">Re-score Articles</button>
   <button class="act" data-run="doctor">Database Doctor</button>
   <button class="act" data-run="export">Export CSV</button>
  </div>
 </details>

 <div style="margin-top:14px;">
  <div style="font-weight:600;font-size:13px;margin-bottom:6px;">Process Log:</div>
  <pre id="out-data">Ready. Click 'Run Full Sync Now' above to update all news and prices.</pre>
 </div>
</section>
<section id="database" class="tab">
 <div class="bar">
  <input id="db-search" placeholder="🔍 Search database (headline, publisher, summary)…" style="flex:1;min-width:240px;">
  <label>Market <select id="db-market">
   <option value="all" selected>All Markets</option>
   <option value="btc">₿ Bitcoin</option>
   <option value="dollar">💵 US Dollar</option>
   <option value="gold">🥇 Gold &amp; Coin</option>
   <option value="macro">🌐 Macro</option>
  </select></label>
  <label><input type="checkbox" id="db-relonly" checked> Relevant</label>
  <label><input type="checkbox" id="db-eventonly"> 🕒 Event Time</label>
  <label><input type="checkbox" id="db-hasbody"> Has Body</label>
  <label>Rows <select id="db-pagesize">
   <option value="15">15</option>
   <option value="25" selected>25</option>
   <option value="50">50</option>
   <option value="100">100</option>
  </select></label>
  <button class="act" id="db-reload">Refresh</button>
  <button class="act" id="db-export-csv" style="font-weight:600;">📥 Export CSV</button>
 </div>

 <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--mut);margin-bottom:8px;">
  <span id="db-records-info">Loading records…</span>
  <span id="db-sort-info">Sorted by: Date (Newest)</span>
 </div>

 <div style="overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;">
  <table class="excel-table" id="db-table">
   <thead>
    <tr>
     <th data-col="id" style="width:45px;">ID ⇕</th>
     <th data-col="published_utc" style="width:115px;">Date (UTC) ⇕</th>
     <th data-col="publisher" style="width:110px;">Publisher ⇕</th>
     <th data-col="title">Headline (Click to open, hover for summary) ⇕</th>
     <th data-col="market_tag" style="width:70px;">Market ⇕</th>
     <th data-col="score" style="width:65px;">Score ⇕</th>
     <th style="width:60px;">Sentiment</th>
     <th data-col="event_lag_hours" style="width:130px;">Event Time Clue ⇕</th>
     <th style="width:75px;">+1h Move</th>
     <th style="width:75px;">+6h Move</th>
     <th style="width:75px;">+24h Move</th>
     <th style="width:60px;">Body</th>
    </tr>
   </thead>
   <tbody id="db-tbody">
    <tr><td colspan="12" style="text-align:center;padding:24px;color:var(--mut);">Loading data...</td></tr>
   </tbody>
  </table>
 </div>

 <div style="display:flex;justify-content:space-between;align-items:center;margin-top:12px;flex-wrap:wrap;gap:8px;">
  <div style="font-size:12px;color:var(--mut);" id="db-page-summary">Page 1 of 1</div>
  <div class="pagination" style="display:flex;gap:6px;align-items:center;">
   <button class="act" id="db-first-page" style="padding:4px 8px;">⏮ First</button>
   <button class="act" id="db-prev-page" style="padding:4px 10px;">◀ Prev</button>
   <span style="font-size:13px;font-weight:600;" id="db-curr-page-num">1</span>
   <button class="act" id="db-next-page" style="padding:4px 10px;">Next ▶</button>
   <button class="act" id="db-last-page" style="padding:4px 8px;">Last ⏭</button>
   <span style="margin-left:8px;font-size:12px;color:var(--mut);">Go to:</span>
   <input type="number" id="db-jump-page" min="1" style="width:50px;padding:3px 6px;font-size:12px;">
  </div>
 </div>
</section>
</main>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let STORIES=[], HORIZONS=[], CANDLES_DATA=[], SHOW_PREDICTION=false, PREDICTION_INFO=null;

async function api(path,opts){
 const r=await fetch(path,opts);const t=await r.text();
 if(!r.ok)throw new Error(t||r.statusText);
 return JSON.parse(t);
}

function el(tag,cls,text){
 const e=document.createElement(tag);if(cls)e.className=cls;
 if(text!==undefined)e.textContent=text;return e;
}

// Nav Tab Switching
$$('#nav button').forEach(b=>b.onclick=()=>{
 $$('#nav button').forEach(x=>x.classList.toggle('on',x===b));
 $$('.tab').forEach(t=>t.classList.toggle('on',t.id===b.dataset.t));
 if(b.dataset.t==='market') loadCandles();
 if(b.dataset.t==='analysis' && !ANALYSIS_LOADED) runVisualPredictions();
 if(b.dataset.t==='database' && !DB_LOADED) loadDatabase();
});

// Live Stats Bar
async function loadStats(){
 try{
  const s=await api('/api/stats');HORIZONS=s.horizons;
  const mkts=Object.values(s.markets||{}).map(m=>`${m.name.split(' ')[0]}: ${m.outcomes} labeled`).join(' · ');
  const evText=s.events ? ` · 🕒 ${s.events} event times` : '';
  $('#stats').textContent=`${s.articles} articles · ${s.relevant} relevant · ${s.bodies} with body${evText} · ${s.outcomes} outcomes (${mkts})`;
 }catch(e){$('#stats').textContent='Error: '+e.message;}
}

// Live Ticker Bar
async function loadTicker(){
 try{
  const r=await api('/api/ticker');
  const box=$('#live-ticker'); box.replaceChildren();
  if(r.dollar_free){
   const p=(r.dollar_free.rial/10).toLocaleString('en-US',{maximumFractionDigits:0});
   const ch=r.dollar_free.change;
   const pill=el('span','score',`💵 $ ${p} T (${ch>0?'+':''}${ch}%)`);
   pill.style.background='var(--card)'; pill.style.border='1px solid var(--line)';
   box.append(pill);
  }
  if(r.gold_emami){
   const p=(r.gold_emami.rial/10).toLocaleString('en-US',{maximumFractionDigits:0});
   const ch=r.gold_emami.change;
   const pill=el('span','score',`🥇 سکه ${p} T (${ch>0?'+':''}${ch}%)`);
   pill.style.background='var(--card)'; pill.style.border='1px solid var(--line)';
   box.append(pill);
  }
  if(r.btc_rls){
   const p=(r.btc_rls.rial/10000000000).toFixed(2);
   const usd=r.btc_usdt?` ($${Math.round(r.btc_usdt.usd).toLocaleString()})`:'';
   const pill=el('span','score',`₿ BTC ${p}B T${usd}`);
   pill.style.background='var(--card)'; pill.style.border='1px solid var(--line)';
   box.append(pill);
  }
 }catch(e){}
}

// Stories
function renderStories(){
 const box=$('#list'), q=$('#q').value.trim().toLowerCase(), rel=$('#relonly').checked;
 const evOnly=$('#eventonly') ? $('#eventonly').checked : false;
 const dirFilter=$('#story-dir') ? $('#story-dir').value : 'all';
 const sortMode=$('#story-sort') ? $('#story-sort').value : 'score_desc';

 box.replaceChildren();

 let items = STORIES.filter(s => {
  if (rel && !s.relevant) return false;
  if (evOnly && !s.event_clue && !s.event_ts) return false;
  if (dirFilter !== 'all') {
   if (dirFilter === 'up' && s.direction !== 'up') return false;
   if (dirFilter === 'down' && s.direction !== 'down') return false;
   if (dirFilter === 'mixed' && s.direction !== 'flat' && s.direction !== 'mixed') return false;
  }
  if (q) {
   const text = (s.title + ' ' + (s.publisher||'') + ' ' + (s.also||'') + ' ' + (s.event_clue||'')).toLowerCase();
   if (!text.includes(q)) return false;
  }
  return true;
 });

 // Sort items
 items.sort((a, b) => {
  if (sortMode === 'score_desc') return (b.score || 0) - (a.score || 0);
  if (sortMode === 'score_asc') return (a.score || 0) - (b.score || 0);
  if (sortMode === 'date_desc') return (b.published || '').localeCompare(a.published || '');
  if (sortMode === 'date_asc') return (a.published || '').localeCompare(b.published || '');
  if (sortMode === 'repeats_desc') return ((b.repeat || 1) - (a.repeat || 1)) || ((b.sources || 1) - (a.sources || 1));
  if (sortMode === 'move_desc') {
   const maxMove = x => Math.max(0, ...Object.values(x.out || {}).map(o => Math.abs(o.pct || 0)));
   return maxMove(b) - maxMove(a);
  }
  return (b.score || 0) - (a.score || 0);
 });

 if ($('#story-count-info')) {
  $('#story-count-info').textContent = `Showing ${items.length} of ${STORIES.length} clustered stories`;
 }
 if ($('#story-sort-info') && $('#story-sort')) {
  $('#story-sort-info').textContent = `Sorted: ${$('#story-sort').selectedOptions[0].text}`;
 }

 if (!items.length) {
  box.append(el('div', 'empty', 'No stories match your search and filter criteria.'));
  return;
 }

 items.forEach((s, idx) => {
  const card = el('div', 'story'), line = el('div');
  const rankCls = idx === 0 ? 'top-1' : idx === 1 ? 'top-2' : idx === 2 ? 'top-3' : '';
  const rankBadge = el('span', 'rank-badge' + (rankCls ? ' ' + rankCls : ''), '#' + (idx + 1));
  line.append(rankBadge);
  line.append(el('span', 'score', (s.score || 0).toFixed(2)));
  const a = el('a', 't', s.title);
  if (/^https?:\/\//.test(s.url)) { a.href = s.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  line.append(a);
  card.append(line);

  const m = el('div', 'meta');
  m.append(el('span', '', s.publisher));
  if (s.published) m.append(el('span', '', s.published.replace('T', ' ').slice(0, 16) + ' UTC'));
  if (s.repeat > 1) m.append(el('span', '', `merged ×${s.repeat} (${s.sources} outlet${s.sources > 1 ? 's' : ''})`));
  m.append(el('span', 'badge', s.relevant ? 'relevant' : 'not relevant'));
  if (s.market_tag) m.append(el('span', 'badge', s.market_tag.toUpperCase()));
  if (s.direction) m.append(el('span', 'badge ' + (s.direction === 'up' || s.direction === 'down' ? s.direction : ''), 'headline: ' + s.direction));
  if (s.event_clue) {
   const lagStr = s.event_lag_hours != null ? ` (${s.event_lag_hours}h lag)` : '';
   const evBadge = el('span', 'badge event-time', `🕒 Event: ${s.event_clue}${lagStr}`);
   if (s.event_utc) evBadge.title = `Statement/event occurred around ${s.event_utc} UTC`;
   m.append(evBadge);
  }
  card.append(m);
  if (s.also) card.append(el('div', 'meta', 'Also: ' + s.also));
  const keys = Object.keys(s.out || {});
  if (keys.length) {
   const o = el('div', 'outs');
   for (const h of keys) {
    const v = s.out[h];
    o.append(el('span', 'badge ' + v.dir, `+${h}h: ${v.dir} ${v.pct > 0 ? '+' : ''}${v.pct}%`));
   }
   card.append(o);
  }
  box.append(card);
 });
}

async function loadStories(){
 $('#list').replaceChildren(el('div','empty','Loading…'));
 try{
  const h=$('#hours').value, m=$('#mfilter').value;
  STORIES=await api(`/api/top?hours=${h}&market=${m}`);renderStories();
 }catch(e){$('#list').replaceChildren(el('div','empty','Error: '+e.message));}
}

// -------------------------------------------------------------
// Interactive Candlestick Chart & Forecast
// -------------------------------------------------------------

async function loadCandles(){
 const h=$('#mhours').value, sym=$('#msymbol').value;
 $('#ch-sym').textContent=sym;
 $('#ch-range').textContent=$('#mhours').selectedOptions[0].text;
 $('#ch-hover-info').textContent='Loading candles…';
 try{
  CANDLES_DATA=await api(`/api/candles?hours=${h}&symbol=${sym}`);
  if(!CANDLES_DATA.length){
   $('#ch-hover-info').textContent='No price candles stored for this window.';
   $('#candle-chart').replaceChildren();
   $('#candle-table-box').replaceChildren();
   return;
  }
  
  if(SHOW_PREDICTION){
   await fetchAndAttachPredictions(sym);
  } else {
   $('#pred-banner').style.display='none';
  }
  
  const displayData=getChartDataset();
  renderInteractiveChart(displayData);
  renderNewsTimeline(displayData);
  renderTable(displayData);
 }catch(e){
  $('#ch-hover-info').textContent='Error loading candles: '+e.message;
 }
}

async function fetchAndAttachPredictions(sym){
 try{
  PREDICTION_INFO=await api(`/api/prediction?symbol=${sym}&hours=72`);
  if(!PREDICTION_INFO || !Object.keys(PREDICTION_INFO.horizons||{}).length){
   $('#pred-banner').style.display='block';
   $('#pred-banner').textContent='ℹ️ No strong prediction evidence found in recent news for '+sym+'.';
   $('#pred-drivers-box').style.display='none';
   return;
  }
  const hzs=PREDICTION_INFO.horizons;
  const parts=Object.values(hzs).map(hz=>`+${hz.horizon}h: <b>${hz.direction}</b> (${hz.pct>0?'+':''}${hz.pct}%)`);
  $('#pred-banner').style.display='block';
  $('#pred-banner').innerHTML=`🔮 <b>${PREDICTION_INFO.name} Forecast:</b> &nbsp; ${parts.join(' &nbsp;·&nbsp; ')} &nbsp; <span style="color:var(--mut);">(${PREDICTION_INFO.stories} stories analyzed)</span>`;

  // Render Driving News
  const drivers = PREDICTION_INFO.key_drivers || [];
  const dBox = $('#pred-drivers-box');
  if(drivers.length > 0){
   dBox.style.display='block';
   let dHtml = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px;">`+
    `<div style="font-weight:700;font-size:13px;color:var(--pred);">📰 News Driving This Forecast (${drivers.length} key stories):</div>`+
    `<div style="font-size:11px;color:var(--mut);">Historical precedents that produced this prediction</div>`+
    `</div><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px;">`;
   
   drivers.forEach(d => {
    const dirClr = d.direction === 'UP' ? 'var(--up)' : (d.direction === 'DOWN' ? 'var(--dn)' : 'var(--fl)');
    const dirBg = d.direction === 'UP' ? 'var(--upbg)' : (d.direction === 'DOWN' ? 'var(--dnbg)' : 'var(--flbg)');
    const evBadge = d.event_clue ? `<span class="badge event-time" style="font-size:10px;">🕒 ${d.event_clue}</span>` : '';
    const timeStr = d.published ? d.published.replace('T',' ').slice(0,16)+' UTC' : '';

    let histHtml = '';
    if(d.matches && d.matches.length > 0){
     histHtml = `<div style="margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);font-size:11px;color:var(--mut);">`+
      `<div style="font-weight:600;margin-bottom:2px;">Similar Past Precedents:</div>`+
      d.matches.map(m => `
       <div style="margin-top:2px;display:flex;justify-content:space-between;gap:6px;font-size:11px;">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">• ${m.date}: ${m.title.slice(0, 32)}…</span>
        <b style="color:${m.pct > 0 ? 'var(--up)' : 'var(--dn)'};white-space:nowrap;">${m.direction.toUpperCase()} ${m.pct > 0 ? '+' : ''}${m.pct}%</b>
       </div>
      `).join('') + `</div>`;
    }

    dHtml += `<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:12px;">`+
     `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;margin-bottom:4px;">`+
     `<a href="${d.url}" target="_blank" rel="noopener noreferrer" style="font-weight:600;color:var(--fg);text-decoration:none;line-height:1.3;">${d.title}</a>`+
     `<span style="background:${dirBg};color:${dirClr};padding:2px 6px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap;">${d.direction} ${d.pct>0?'+':''}${d.pct}%</span>`+
     `</div>`+
     `<div style="color:var(--mut);font-size:11px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+
     `<span>${d.publisher}</span>`+
     `<span>${timeStr}</span>`+
     `${evBadge}`+
     `<span style="margin-left:auto;">Score: <b>${d.score}</b></span>`+
     `</div>`+
     `${histHtml}`+
     `</div>`;
   });
   dHtml += `</div>`;
   dBox.innerHTML = dHtml;
  } else {
   dBox.style.display='none';
  }
 }catch(e){
  $('#pred-banner').style.display='block';
  $('#pred-banner').textContent='Error fetching prediction: '+e.message;
  $('#pred-drivers-box').style.display='none';
 }
}

function getChartDataset(){
 if(!SHOW_PREDICTION || !PREDICTION_INFO || !PREDICTION_INFO.horizons || !CANDLES_DATA.length){
  return CANDLES_DATA.map(d=>({...d, is_pred:false}));
 }
 const last=CANDLES_DATA[CANDLES_DATA.length-1];
 const dt=CANDLES_DATA.length>1?(last.ts-CANDLES_DATA[CANDLES_DATA.length-2].ts):3600;
 const out=CANDLES_DATA.map(d=>({...d, is_pred:false}));

 const hzs=PREDICTION_INFO.horizons;
 let prevClose=last.close;
 
 const horizonsSorted=Object.keys(hzs).map(Number).sort((a,b)=>a-b);
 horizonsSorted.forEach(h=>{
  const hz=hzs[h];
  const targetClose=last.close*(1+hz.pct/100);
  const candleTime=last.ts + h*3600;
  out.push({
   ts: candleTime,
   open: prevClose,
   close: targetClose,
   high: Math.max(prevClose, targetClose)*1.001,
   low: Math.min(prevClose, targetClose)*0.999,
   is_pred: true,
   horizon: h,
   direction: hz.direction,
   pct: hz.pct,
   confidence: hz.confidence
  });
  prevClose=targetClose;
 });
 return out;
}

function renderInteractiveChart(data){
 const svg=$('#candle-chart');
 svg.replaceChildren();
 const w=svg.clientWidth||1100, h=320, padL=40, padR=70, padT=25, padB=58;
 const chartW=w-padL-padR, chartH=h-padT-padB;

 const allPrices=data.flatMap(d=>[d.open,d.close,d.high||d.open,d.low||d.close]);
 const minP=Math.min(...allPrices);
 const maxP=Math.max(...allPrices)||1;
 const range=(maxP-minP)||1;

 const getY=p=>padT+chartH-((p-minP)/range)*chartH;
 const step=chartW/data.length;
 const barW=Math.max(2, Math.min(18, step*0.75));

 // Grid Lines
 for(let i=0; i<=4; i++){
  const priceLevel=minP + (range/4)*i;
  const y=getY(priceLevel);
  const gl=document.createElementNS('http://www.w3.org/2000/svg','line');
  gl.setAttribute('x1',padL); gl.setAttribute('y1',y);
  gl.setAttribute('x2',w-padR); gl.setAttribute('y2',y);
  gl.setAttribute('stroke','var(--line)'); gl.setAttribute('stroke-dasharray','2,4');
  gl.setAttribute('opacity','0.6');
  svg.appendChild(gl);

  const txt=document.createElementNS('http://www.w3.org/2000/svg','text');
  txt.setAttribute('x',w-padR+8); txt.setAttribute('y',y+4);
  txt.setAttribute('fill','var(--mut)'); txt.setAttribute('font-size','10');
  txt.setAttribute('font-family','ui-monospace,monospace');
  txt.textContent=Math.round(priceLevel).toLocaleString();
  svg.appendChild(txt);
 }

 // Forecast Separation Line if predictions exist
 const firstPredIdx=data.findIndex(d=>d.is_pred);
 if(firstPredIdx>0){
  const sepX=padL+(firstPredIdx-0.5)*step;
  const sepL=document.createElementNS('http://www.w3.org/2000/svg','line');
  sepL.setAttribute('x1',sepX); sepL.setAttribute('y1',padT);
  sepL.setAttribute('x2',sepX); sepL.setAttribute('y2',h-padB);
  sepL.setAttribute('stroke','var(--pred)'); sepL.setAttribute('stroke-dasharray','4,3');
  sepL.setAttribute('stroke-width','1.5');
  svg.appendChild(sepL);

  const sepTxt=document.createElementNS('http://www.w3.org/2000/svg','text');
  sepTxt.setAttribute('x',sepX+4); sepTxt.setAttribute('y',padT+12);
  sepTxt.setAttribute('fill','var(--pred)'); sepTxt.setAttribute('font-size','11');
  sepTxt.setAttribute('font-weight','700');
  sepTxt.textContent='🔮 Forecast ➔';
  svg.appendChild(sepTxt);
 }

 // Candles (Wicks + Bodies)
 data.forEach((d,idx)=>{
  const xCenter=padL+idx*step+step/2;
  const xBar=xCenter-barW/2;
  const yO=getY(d.open), yC=getY(d.close);
  const yH=getY(d.high||Math.max(d.open,d.close));
  const yL=getY(d.low||Math.min(d.open,d.close));
  const isUp=d.close>=d.open;
  let color=isUp?'var(--up)':'var(--dn)';

  // Wick
  const wick=document.createElementNS('http://www.w3.org/2000/svg','line');
  wick.setAttribute('x1',xCenter); wick.setAttribute('y1',yH);
  wick.setAttribute('x2',xCenter); wick.setAttribute('y2',yL);
  wick.setAttribute('stroke',d.is_pred?'var(--pred)':color);
  wick.setAttribute('stroke-width','1');
  svg.appendChild(wick);

  // Body
  const bar=document.createElementNS('http://www.w3.org/2000/svg','rect');
  bar.setAttribute('x',xBar);
  bar.setAttribute('y',Math.min(yO,yC));
  bar.setAttribute('width',barW);
  bar.setAttribute('height',Math.max(2,Math.abs(yC-yO)));
  
  if(d.is_pred){
   bar.setAttribute('fill',isUp?'rgba(34,197,94,0.3)':'rgba(239,68,68,0.3)');
   bar.setAttribute('stroke',color);
   bar.setAttribute('stroke-width','1.5');
   bar.setAttribute('stroke-dasharray','3,2');

   // Horizon label
   const hTag=document.createElementNS('http://www.w3.org/2000/svg','text');
   hTag.setAttribute('x',xCenter); hTag.setAttribute('y',Math.min(yO,yC)-6);
   hTag.setAttribute('fill',color); hTag.setAttribute('font-size','10');
   hTag.setAttribute('font-weight','700'); hTag.setAttribute('text-anchor','middle');
   hTag.textContent=`+${d.horizon}h`;
   svg.appendChild(hTag);
  } else {
   bar.setAttribute('fill',color);
  }
  bar.id=`candle-${idx}`;
  svg.appendChild(bar);


 });

 // Chart Baseline
 const baseline=document.createElementNS('http://www.w3.org/2000/svg','line');
 baseline.setAttribute('x1',padL); baseline.setAttribute('y1',h-padB);
 baseline.setAttribute('x2',w-padR); baseline.setAttribute('y2',h-padB);
 baseline.setAttribute('stroke','var(--line)');
 svg.appendChild(baseline);

 // Date Ticks along timeline
 const labelStep=Math.max(1, Math.round(95/step));
 data.forEach((d, idx)=>{
  if(idx % labelStep === 0 || idx === data.length - 1){
   const xCenter=padL+idx*step+step/2;
   const dTxt=document.createElementNS('http://www.w3.org/2000/svg','text');
   dTxt.setAttribute('x',xCenter); dTxt.setAttribute('y',h-padB+15);
   dTxt.setAttribute('fill','var(--mut)'); dTxt.setAttribute('font-size','10');
   dTxt.setAttribute('font-family','ui-monospace,monospace');
   dTxt.setAttribute('text-anchor','middle');
   const dtObj=new Date(d.ts*1000);
   dTxt.textContent=dtObj.toISOString().slice(5,10)+' '+dtObj.toISOString().slice(11,16);
   svg.appendChild(dTxt);
  }
 });

 // News Event Line Track below the candles
 data.forEach((d, idx)=>{
  if(d.news && d.news.length > 0){
   const xCenter=padL+idx*step+step/2;

   // Connector line from baseline down to news badge
   const tick=document.createElementNS('http://www.w3.org/2000/svg','line');
   tick.setAttribute('x1',xCenter); tick.setAttribute('y1',h-padB);
   tick.setAttribute('x2',xCenter); tick.setAttribute('y2',h-padB+24);
   tick.setAttribute('stroke','var(--pred)');
   tick.setAttribute('stroke-dasharray','2,2');
   tick.setAttribute('opacity','0.85');
   svg.appendChild(tick);

   // News Badge sitting on the timeline track below the chart
   const nG=document.createElementNS('http://www.w3.org/2000/svg','g');
   nG.style.cursor='pointer';

   const bgCircle=document.createElementNS('http://www.w3.org/2000/svg','circle');
   bgCircle.setAttribute('cx',xCenter);
   bgCircle.setAttribute('cy',h-padB+34);
   bgCircle.setAttribute('r','9');
   bgCircle.setAttribute('fill','var(--predbg)');
   bgCircle.setAttribute('stroke','var(--pred)');
   bgCircle.setAttribute('stroke-width','1.5');
   nG.appendChild(bgCircle);

   const icon=document.createElementNS('http://www.w3.org/2000/svg','text');
   icon.setAttribute('x',xCenter);
   icon.setAttribute('y',h-padB+37);
   icon.setAttribute('font-size','9');
   icon.setAttribute('font-weight','700');
   icon.setAttribute('fill','var(--pred)');
   icon.setAttribute('text-anchor','middle');
   icon.textContent=d.news.length > 1 ? String(d.news.length) : 'N';
   nG.appendChild(icon);

   nG.onmouseenter=()=>{
    bgCircle.setAttribute('fill','var(--pred)');
    icon.setAttribute('fill','#fff');
    const cBar=$(`#candle-${idx}`);
    if(cBar){ cBar.style.stroke='var(--pred)'; cBar.style.strokeWidth='2.5px'; }
    $('#ch-hover-info').innerHTML=`📰 <b>[${d.news[0].publisher}]</b> ${d.news[0].title}`;
   };
   nG.onmouseleave=()=>{
    bgCircle.setAttribute('fill','var(--predbg)');
    icon.setAttribute('fill','var(--pred)');
    const cBar=$(`#candle-${idx}`);
    if(cBar){ cBar.style.stroke=''; cBar.style.strokeWidth=''; }
   };
   nG.onclick=(e)=>{
    e.stopPropagation();
    const cBox=$('#candle-selected-news');
    if(cBox){
     cBox.style.display='block';
     let html=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">`+
      `<div style="font-weight:700;font-size:13px;color:var(--pred);">📰 News Catalyst (${new Date(d.ts*1000).toISOString().replace('T',' ').slice(0,16)} UTC):</div>`+
      `<button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>`+
      `</div>`;
     d.news.forEach(n=>{
      const ev=n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : '';
      const dir=n.direction ? `<span class="badge ${n.direction}">headline: ${n.direction}</span>` : '';
      html+=`<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;">`+
       `<div style="font-weight:600;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>`+
       `<div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+
       `<span>${n.publisher}</span>`+
       `<span>Published: ${n.published ? n.published.replace('T',' ').slice(0,16)+' UTC' : ''}</span>`+
       `${dir} ${ev}`+
       `<span style="margin-left:auto;">Relevancy: <b>${n.score}</b></span>`+
       `</div></div>`;
     });
     cBox.innerHTML=html;
     cBox.scrollIntoView({behavior:'smooth', block:'nearest'});
    }
   };
   svg.appendChild(nG);
  }
 });

 // Crosshair Elements
 const chX=document.createElementNS('http://www.w3.org/2000/svg','line');
 chX.setAttribute('stroke','var(--mut)'); chX.setAttribute('stroke-dasharray','3,3');
 chX.setAttribute('opacity','0'); chX.setAttribute('y1',padT); chX.setAttribute('y2',h-padB);
 svg.appendChild(chX);

 const chY=document.createElementNS('http://www.w3.org/2000/svg','line');
 chY.setAttribute('stroke','var(--mut)'); chY.setAttribute('stroke-dasharray','3,3');
 chY.setAttribute('opacity','0'); chY.setAttribute('x1',padL); chY.setAttribute('x2',w-padR);
 svg.appendChild(chY);

 // Interactive Hover Layer
 const overlay=document.createElementNS('http://www.w3.org/2000/svg','rect');
 overlay.setAttribute('x',padL); overlay.setAttribute('y',padT);
 overlay.setAttribute('width',chartW); overlay.setAttribute('height',chartH+50);
 overlay.setAttribute('fill','transparent');
 svg.appendChild(overlay);

 const tooltip=$('#tooltip');

 overlay.onmousemove=(e)=>{
  const rect=svg.getBoundingClientRect();
  const mx=e.clientX-rect.left;
  const my=e.clientY-rect.top;
  const idx=Math.min(data.length-1, Math.max(0, Math.floor((mx-padL)/step)));
  const d=data[idx];
  if(!d) return;

  const xCenter=padL+idx*step+step/2;
  const yC=getY(d.close);

  // Position crosshairs
  chX.setAttribute('x1',xCenter); chX.setAttribute('x2',xCenter);
  chX.setAttribute('opacity','0.7');
  chY.setAttribute('y1',yC); chY.setAttribute('y2',yC);
  chY.setAttribute('opacity','0.7');

  // Update top header bar
  const dateStr=new Date(d.ts*1000).toISOString().replace('T',' ').slice(0,16)+' UTC';
  const delta=(((d.close-d.open)/d.open)*100).toFixed(2);
  const deltaClr=delta>0?'var(--up)':'var(--dn)';
  
  let newsBox = '';
  if(d.news && d.news.length > 0){
   newsBox = `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--line);max-width:300px;">`+
    `<div style="font-weight:700;color:var(--pred);font-size:11px;margin-bottom:3px;">📰 News Event Catalyst (${d.news.length}):</div>`;
   d.news.slice(0, 2).forEach(n => {
    const ev = n.event_clue ? `<span class="badge event-time" style="font-size:9px;">🕒 ${n.event_clue}</span>` : '';
    const dir = n.direction ? `<span class="badge ${n.direction}" style="font-size:9px;">${n.direction}</span>` : '';
    newsBox += `<div style="font-size:11px;margin-bottom:3px;line-height:1.3;">`+
     `<b>[${n.publisher}]</b> ${n.title.slice(0, 60)} ${dir} ${ev}</div>`;
   });
   newsBox += `<div style="font-size:10px;color:var(--mut);">Click candle for full story</div></div>`;
  }

  if(d.is_pred){
   let predInfo=`<span class="badge pred">🔮 PREDICTED +${d.horizon}h</span> &nbsp; `+
    `Time: <b>${dateStr}</b> · Target Close: <b>${Math.round(d.close).toLocaleString()}</b> · `+
    `Forecast: <b style="color:${deltaClr}">${d.direction} (${delta>0?'+':''}${delta}%)</b>`;
   if(PREDICTION_INFO && PREDICTION_INFO.key_drivers && PREDICTION_INFO.key_drivers.length > 0){
    const kd=PREDICTION_INFO.key_drivers[0];
    predInfo += ` · <span style="color:var(--pred);">Derived from: <b>[${kd.publisher}]</b> ${kd.title.slice(0, 40)}…</span>`;
   }
   $('#ch-hover-info').innerHTML=predInfo;
   tooltip.innerHTML=`<div style="font-weight:700;color:var(--pred);margin-bottom:4px;">🔮 Forecast Candle (+${d.horizon}h)</div>
    <div>Time: <b>${dateStr}</b></div>
    <div>Expected Close: <b>${Math.round(d.close).toLocaleString()}</b></div>
    <div>Signal: <b style="color:${deltaClr}">${d.direction} (${delta>0?'+':''}${delta}%)</b></div>
    <div style="color:var(--mut);">Confidence: ${d.confidence||'MODERATE'}</div>
    ${newsBox}`;
  } else {
   let histInfo=`Date: <b>${dateStr}</b> · `+
    `Open: <b>${Math.round(d.open).toLocaleString()}</b> · `+
    `Close: <b>${Math.round(d.close).toLocaleString()}</b> · `+
    `Delta: <b style="color:${deltaClr}">${delta>0?'+':''}${delta}%</b>`;
   if(d.news && d.news.length > 0){
    const tn=d.news[0];
    const more=d.news.length > 1 ? ` (+${d.news.length-1})` : '';
    histInfo += ` · <span style="color:var(--pred);font-weight:600;">📰 [${tn.publisher}] ${tn.title.slice(0, 45)}…${more}</span>`;
   }
   $('#ch-hover-info').innerHTML=histInfo;
   tooltip.innerHTML=`<div style="color:var(--mut);margin-bottom:3px;">${dateStr}</div>
    <div>Open: <b>${Math.round(d.open).toLocaleString()}</b></div>
    <div>Close: <b>${Math.round(d.close).toLocaleString()}</b></div>
    <div>Change: <b style="color:${deltaClr}">${delta>0?'+':''}${delta}%</b></div>
    ${newsBox}`;
  }

  // Floating tooltip
  tooltip.style.display='block';
  tooltip.style.left=Math.min(rect.width-280, Math.max(10, mx+16))+'px';
  tooltip.style.top=Math.min(rect.height-140, Math.max(10, my-20))+'px';
 };

 overlay.onclick=(e)=>{

  const rect=svg.getBoundingClientRect();

  const mx=e.clientX-rect.left;

  const idx=Math.min(data.length-1, Math.max(0, Math.floor((mx-padL)/step)));

  const d=data[idx];

  if(!d) return;

  const cBox=$('#candle-selected-news');

  if(d.news && d.news.length > 0){

   cBox.style.display='block';

   let html=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">`+

    `<div style="font-weight:700;font-size:13px;color:var(--pred);">📰 News Catalyst for Candle at ${new Date(d.ts*1000).toISOString().replace('T',' ').slice(0,16)} UTC:</div>`+

    `<button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>`+

    `</div>`;

   d.news.forEach(n => {

    const ev = n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : '';

    const dir = n.direction ? `<span class="badge ${n.direction}">headline: ${n.direction}</span>` : '';

    html += `<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;">`+

     `<div style="font-weight:600;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>`+

     `<div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+

     `<span>${n.publisher}</span>`+

     `<span>${n.published ? n.published.replace('T',' ').slice(0,16)+' UTC' : ''}</span>`+

     `${dir} ${ev}`+

     `<span style="margin-left:auto;">Relevancy: <b>${n.score}</b></span>`+

     `</div></div>`;

   });

   cBox.innerHTML=html;

   cBox.scrollIntoView({behavior:'smooth', block:'nearest'});

  } else if(d.is_pred && PREDICTION_INFO && PREDICTION_INFO.key_drivers && PREDICTION_INFO.key_drivers.length > 0){

   cBox.style.display='block';

   let html=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">`+

    `<div style="font-weight:700;font-size:13px;color:var(--pred);">🔮 News Driving Forecast (+${d.horizon}h):</div>`+

    `<button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>`+

    `</div>`;

   PREDICTION_INFO.key_drivers.forEach(n => {

    const ev = n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : '';

    html += `<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;">`+

     `<div style="font-weight:600;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>`+

     `<div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+

     `<span>${n.publisher}</span>`+

     `<span>Signal: <b>${n.direction} (${n.pct>0?'+':''}${n.pct}%)</b></span>`+

     `${ev}`+

     `<span style="margin-left:auto;">Score: <b>${n.score}</b></span>`+

     `</div></div>`;

   });

   cBox.innerHTML=html;

   cBox.scrollIntoView({behavior:'smooth', block:'nearest'});

  }

 };



 overlay.onclick=(e)=>{
  const rect=svg.getBoundingClientRect();
  const mx=e.clientX-rect.left;
  const idx=Math.min(data.length-1, Math.max(0, Math.floor((mx-padL)/step)));
  const d=data[idx];
  if(!d) return;
  const cBox=$('#candle-selected-news');
  if(d.news && d.news.length > 0){
   cBox.style.display='block';
   let html=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">`+
    `<div style="font-weight:700;font-size:13px;color:var(--pred);">📰 News Catalyst for Candle at ${new Date(d.ts*1000).toISOString().replace('T',' ').slice(0,16)} UTC:</div>`+
    `<button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>`+
    `</div>`;
   d.news.forEach(n => {
    const ev = n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : '';
    const dir = n.direction ? `<span class="badge ${n.direction}">headline: ${n.direction}</span>` : '';
    html += `<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;">`+
     `<div style="font-weight:600;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>`+
     `<div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+
     `<span>${n.publisher}</span>`+
     `<span>${n.published ? n.published.replace('T',' ').slice(0,16)+' UTC' : ''}</span>`+
     `${dir} ${ev}`+
     `<span style="margin-left:auto;">Relevancy: <b>${n.score}</b></span>`+
     `</div></div>`;
   });
   cBox.innerHTML=html;
   cBox.scrollIntoView({behavior:'smooth', block:'nearest'});
  } else if(d.is_pred && PREDICTION_INFO && PREDICTION_INFO.key_drivers && PREDICTION_INFO.key_drivers.length > 0){
   cBox.style.display='block';
   let html=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">`+
    `<div style="font-weight:700;font-size:13px;color:var(--pred);">🔮 News Driving Forecast (+${d.horizon}h):</div>`+
    `<button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>`+
    `</div>`;
   PREDICTION_INFO.key_drivers.forEach(n => {
    const ev = n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : '';
    html += `<div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;">`+
     `<div style="font-weight:600;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>`+
     `<div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`+
     `<span>${n.publisher}</span>`+
     `<span>Signal: <b>${n.direction} (${n.pct>0?'+':''}${n.pct}%)</b></span>`+
     `${ev}`+
     `<span style="margin-left:auto;">Score: <b>${n.score}</b></span>`+
     `</div></div>`;
   });
   cBox.innerHTML=html;
   cBox.scrollIntoView({behavior:'smooth', block:'nearest'});
  }
 };

 overlay.onmouseleave=()=>{
  chX.setAttribute('opacity','0');
  chY.setAttribute('opacity','0');
  tooltip.style.display='none';
  $('#ch-hover-info').textContent='Hover over any candle for exact price';
 };
}

function renderNewsTimeline(data){
 const track=$('#news-timeline-track');
 const countTag=$('#news-count-tag');
 if(!track) return;
 track.replaceChildren();

 const allNews=[];
 const seen=new Set();
 data.forEach((d, idx)=>{
  if(d.news && d.news.length > 0){
   d.news.forEach(n=>{
    if(!seen.has(n.id)){
     seen.add(n.id);
     allNews.push({...n, candleIdx: idx, candleTs: d.ts, open: d.open, close: d.close});
    }
   });
  }
 });

 if(countTag) countTag.textContent = allNews.length ? `${allNews.length} news events mapped to this window` : 'No news in this window';

 if(!allNews.length){
  track.innerHTML = '<span style="color:var(--mut);font-size:12px;padding:4px 0;">No relevant news recorded during this timeframe.</span>';
  return;
 }

 allNews.forEach(n=>{
  const dStr=new Date(n.candleTs * 1000).toISOString().replace('T',' ').slice(5, 16) + ' UTC';
  const delta=(((n.close - n.open) / n.open) * 100).toFixed(2);
  const deltaClr=delta > 0 ? 'var(--up)' : (delta < 0 ? 'var(--dn)' : 'var(--fl)');
  const dirBadge=n.direction ? `<span class="badge ${n.direction}" style="font-size:9px;padding:1px 4px;">${n.direction}</span>` : '';
  const evBadge=n.event_clue ? `<span class="badge event-time" style="font-size:9px;padding:1px 4px;">🕒 ${n.event_clue}</span>` : '';

  const pill=document.createElement('div');
  pill.id=`timeline-news-${n.id}`;
  pill.style.cssText='flex:0 0 auto;max-width:280px;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:11px;cursor:pointer;transition:border-color 0.15s ease, transform 0.15s ease;';
  pill.innerHTML=`
   <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:3px;color:var(--mut);font-size:10px;">
    <span><b>${dStr}</b></span>
    <b style="color:${deltaClr};">${delta > 0 ? '+' : ''}${delta}%</b>
   </div>
   <div style="font-weight:600;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:2px;" title="${n.title}">
    <span style="color:var(--pred);">[${n.publisher}]</span> ${n.title}
   </div>
   <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;">
    ${dirBadge} ${evBadge}
    <span style="margin-left:auto;color:var(--mut);">Sc: ${n.score}</span>
   </div>
  `;

  // Hover highlights corresponding candle
  pill.onmouseenter=()=>{
   pill.style.borderColor='var(--pred)';
   pill.style.transform='translateY(-2px)';
   const cBar=$(`#candle-${n.candleIdx}`);
   if(cBar){
    cBar.style.stroke='var(--pred)';
    cBar.style.strokeWidth='2.5px';
   }
  };
  pill.onmouseleave=()=>{
   pill.style.borderColor='var(--line)';
   pill.style.transform='none';
   const cBar=$(`#candle-${n.candleIdx}`);
   if(cBar){
    cBar.style.stroke='';
    cBar.style.strokeWidth='';
   }
  };

  // Click displays full story in detail card
  pill.onclick=()=>{
   const cBox=$('#candle-selected-news');
   if(cBox){
    cBox.style.display='block';
    cBox.innerHTML=`
     <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <div style="font-weight:700;font-size:13px;color:var(--pred);">📰 News Catalyst (${new Date(n.candleTs*1000).toISOString().replace('T',' ').slice(0,16)} UTC):</div>
      <button class="act" onclick="$('#candle-selected-news').style.display='none'" style="padding:2px 8px;font-size:11px;">✕ Close</button>
     </div>
     <div style="background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;">
      <div style="font-weight:600;font-size:13px;"><a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);">${n.title}</a></div>
      <div style="font-size:12px;color:var(--mut);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
       <span>${n.publisher}</span>
       <span>Published: ${n.published ? n.published.replace('T',' ').slice(0,16)+' UTC' : ''}</span>
       ${n.direction ? `<span class="badge ${n.direction}">headline: ${n.direction}</span>` : ''}
       ${n.event_clue ? `<span class="badge event-time">🕒 Event: ${n.event_clue}</span>` : ''}
       <span style="margin-left:auto;">Relevancy: <b>${n.score}</b></span>
      </div>
     </div>
    `;
    cBox.scrollIntoView({behavior:'smooth', block:'nearest'});
   }
  };

  track.appendChild(pill);
 });
}

function renderTable(data){
 const box=$('#candle-table-box');
 const rows=[...data].reverse().slice(0,50);
 let html='<table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">'+
  '<thead><tr style="border-bottom:1px solid var(--line);color:var(--mut);">'+
  '<th style="padding:6px">UTC Date</th><th>Status</th><th>Open</th><th>Close</th><th>Delta</th><th>Catalyst / Related News</th></tr></thead><tbody>';
 for(const r of rows){
  const d=new Date(r.ts*1000).toISOString().replace('T',' ').slice(0,16);
  const delta=(((r.close-r.open)/r.open)*100).toFixed(2);
  const clr=delta>0?'var(--up)':(delta<0?'var(--dn)':'var(--fl)');
  const statusBadge=r.is_pred?`<span class="badge pred">🔮 PREDICTED +${r.horizon}h</span>`:'<span class="badge">HISTORICAL</span>';
  const rowBg=r.is_pred?'style="background:var(--predbg);"':'';
  
  let newsCell = '<span style="color:var(--mut);">—</span>';
  if(r.news && r.news.length > 0){
   const n = r.news[0];
   const more = r.news.length > 1 ? ` <span class="badge" style="font-size:10px;">+${r.news.length-1} more</span>` : '';
   const ev = n.event_clue ? `<span class="badge event-time" style="font-size:10px;">🕒 ${n.event_clue}</span>` : '';
   newsCell = `<a href="${n.url}" target="_blank" rel="noopener noreferrer" style="color:var(--fg);text-decoration:none;font-weight:500;">`+
    `📰 <b>[${n.publisher}]</b> ${n.title.slice(0, 50)}…</a> ${ev} ${more}`;
  } else if(r.is_pred && PREDICTION_INFO && PREDICTION_INFO.key_drivers && PREDICTION_INFO.key_drivers.length > 0){
   const kd = PREDICTION_INFO.key_drivers[0];
   newsCell = `<span style="color:var(--pred);">🔮 Derived from: <b>[${kd.publisher}]</b> ${kd.title.slice(0, 45)}…</span>`;
  }

  html+=`<tr style="border-bottom:1px solid var(--line);" ${rowBg}>
   <td style="padding:6px">${d}</td>
   <td>${statusBadge}</td>
   <td>${Math.round(r.open).toLocaleString()}</td>
   <td>${Math.round(r.close).toLocaleString()}</td>
   <td style="color:${clr};font-weight:600;">${delta>0?'+':''}${delta}%</td>
   <td style="padding:6px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${newsCell}</td>
  </tr>`;
 }
 html+='</tbody></table>';
 box.innerHTML=html;
}

// -------------------------------------------------------------
// Quick Sync & Data Management
// -------------------------------------------------------------

async function run(cmd,args,outId){
 const out=$('#'+outId),btns=$$('button.act');
 btns.forEach(b=>b.disabled=true);
 if(out) out.textContent='Running '+cmd+'… (please wait)';
 try{
  const r=await api('/api/run',{
   method:'POST',
   headers:{'Content-Type':'application/json','X-UI':'1'},
   body:JSON.stringify({cmd,args})
  });
  if(out) out.textContent=r.output;
 }catch(e){
  if(out) out.textContent='Error: '+e.message;
 }
 btns.forEach(b=>b.disabled=false);
 await loadStats();
 await loadTicker();
 await loadStories();
 await loadCandles();
}

async function startFullSync(){
 const pBox=$('#sync-progress'), pMsg=$('#sync-status-msg');
 pBox.style.display='block';
 pMsg.textContent='🚀 Starting full multi-market sync (collect news + bodies + price labels)…';
 $('#out-data').textContent='Initiating full sync…';

 const steps=[$('#st-1'),$('#st-2'),$('#st-3'),$('#st-4'),$('#st-5')];
 steps.forEach(s=>s.className='step-pill');
 $('#st-1').className='step-pill active';

 const includeBodies=$('#sync-scrape-bodies').checked;
 const runPreds=$('#sync-run-predictions').checked;

 const btn=$('#btn-full-sync'), quickBtn=$('#header-quick-sync');
 btn.disabled=quickBtn.disabled=true;

 try{
  const r=await api('/api/run',{
   method:'POST',
   headers:{'Content-Type':'application/json','X-UI':'1'},
   body:JSON.stringify({
    cmd:'sync_all',
    args:{include_bodies:includeBodies, limit:40, run_predictions:runPreds}
   })
  });
  $('#out-data').textContent=r.output;
  steps.forEach(s=>s.className='step-pill done');
  pMsg.textContent='✓ Full sync finished successfully! All 3 markets updated.';
 }catch(e){
  $('#out-data').textContent='Error: '+e.message;
  pMsg.textContent='❌ Sync failed: '+e.message;
 }
 btn.disabled=quickBtn.disabled=false;
 await loadStats();
 await loadTicker();
 await loadStories();
 await loadCandles();
}

// =============================================================
// PREDICT & BACKTEST VISUAL TAB
// =============================================================
let ANALYSIS_VIEW = 'predict';
let ANALYSIS_LOADED = false;

async function runVisualPredictions(){
 ANALYSIS_VIEW = 'predict';
 $('#predict-results-view').style.display = 'block';
 $('#backtest-results-view').style.display = 'none';
 $('#predict-results-view').replaceChildren(el('div', 'empty', '🔮 Calculating multi-market similarity predictions from recent news…'));
 $('#btn-run-predictions').disabled = true;

 try {
  const h = $('#phours').value, m = $('#pmarket').value;
  const preds = await api(`/api/predict_view?hours=${h}&market=${m}`);
  ANALYSIS_LOADED = true;
  const box = $('#predict-results-view');
  box.replaceChildren();

  if (!preds || !preds.length) {
   box.append(el('div', 'empty', 'No predictions available. Try syncing feeds first.'));
   $('#btn-run-predictions').disabled = false;
   return;
  }

  for (const mkt of preds) {
   const card = el('div', 'mkt-card');
   const head = el('div');
   head.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;border-bottom:1px solid var(--line);padding-bottom:10px;flex-wrap:wrap;gap:8px;';
   
   const titleBox = el('div');
   const title = el('h3', '', mkt.name + ' ');
   title.style.cssText = 'margin:0;font-size:16px;display:inline-block;';
   const symSpan = el('span', '', '(' + mkt.symbol + ')');
   symSpan.style.cssText = 'font-size:12px;color:var(--mut);font-weight:normal;margin-left:4px;';
   titleBox.append(title, symSpan);

   const badgeBox = el('div');
   badgeBox.append(
    el('span', 'badge', `${mkt.stories} recent stories`),
    el('span', 'badge', `${mkt.history_count} historical precedents`)
   );
   head.append(titleBox, badgeBox);
   card.append(head);

   // Horizon Grid
   const grid = el('div', 'horizon-grid');
   const hzKeys = ['1', '6', '24'].filter(k => mkt.horizons && mkt.horizons[k]);

   if (!hzKeys.length) {
    grid.append(el('div', 'empty', 'Insufficient historical outcome data for this market.'));
   } else {
    for (const k of hzKeys) {
     const hz = mkt.horizons[k];
     const hCard = el('div', 'horizon-card');
     
     const topRow = el('div');
     topRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;';
     topRow.append(el('span', '', `+${hz.horizon}h Forecast`));
     topRow.querySelector('span').style.fontWeight = '700';

     const sigCls = hz.direction === 'UP' ? 'UP' : hz.direction === 'DOWN' ? 'DOWN' : 'FLAT';
     const sigText = hz.direction === 'UP' ? '🟢 BULLISH UP' : hz.direction === 'DOWN' ? '🔴 BEARISH DOWN' : '⚪ NEUTRAL FLAT';
     const sigPill = el('span', `signal-pill ${sigCls}`, sigText);
     topRow.append(sigPill);
     hCard.append(topRow);

     // Expected move
     const moveRow = el('div');
     moveRow.style.cssText = 'margin-top:10px;font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;';
     moveRow.style.color = hz.pct > 0 ? 'var(--up)' : hz.pct < 0 ? 'var(--dn)' : 'var(--fl)';
     moveRow.textContent = `${hz.pct > 0 ? '+' : ''}${hz.pct.toFixed(2)}% `;
     const moveSub = el('span', '', 'exp. move');
     moveSub.style.cssText = 'font-size:11px;color:var(--mut);font-weight:normal;margin-left:4px;';
     moveRow.append(moveSub);
     hCard.append(moveRow);

     // Prob track
     const probBox = el('div');
     probBox.style.marginTop = '10px';
     const pLabels = el('div');
     pLabels.style.cssText = 'display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-bottom:3px;';
     pLabels.append(el('span', '', 'Historical Odds'), el('span', '', `${hz.confidence} Confidence`));
     probBox.append(pLabels);

     const track = el('div', 'prob-track');
     const uBar = el('div'); uBar.style.width = (hz.up || 0) + '%'; uBar.style.background = 'var(--up)'; uBar.title = `UP: ${hz.up}%`;
     const fBar = el('div'); fBar.style.width = (hz.flat || 0) + '%'; fBar.style.background = 'var(--fl)'; fBar.title = `FLAT: ${hz.flat}%`;
     const dBar = el('div'); dBar.style.width = (hz.down || 0) + '%'; dBar.style.background = 'var(--dn)'; dBar.title = `DOWN: ${hz.down}%`;
     track.append(uBar, fBar, dBar);
     probBox.append(track);

     const subLeg = el('div');
     subLeg.style.cssText = 'display:flex;justify-content:space-between;font-size:10px;margin-top:4px;';
     const uLbl = el('span', '', `UP ${(hz.up || 0).toFixed(0)}%`); uLbl.style.color = 'var(--up)';
     const fLbl = el('span', '', `FLAT ${(hz.flat || 0).toFixed(0)}%`); fLbl.style.color = 'var(--fl)';
     const dLbl = el('span', '', `DOWN ${(hz.down || 0).toFixed(0)}%`); dLbl.style.color = 'var(--dn)';
     subLeg.append(uLbl, fLbl, dLbl);
     probBox.append(subLeg);

     hCard.append(probBox);
     grid.append(hCard);
    }
   }
   card.append(grid);

   // Drivers Accordion
   const drivers = mkt.key_drivers || [];
   if (drivers.length) {
    const details = document.createElement('details');
    details.style.cssText = 'margin-top:14px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 12px;';
    const summary = document.createElement('summary');
    summary.style.cssText = 'cursor:pointer;font-weight:600;font-size:12px;user-select:none;color:var(--fg);';
    summary.textContent = `📰 Key Driving News Precedents (${drivers.length} stories)`;
    details.append(summary);

    const dList = el('div');
    dList.style.cssText = 'margin-top:10px;display:flex;flex-direction:column;gap:8px;';

    drivers.forEach(dr => {
     const dItem = el('div');
     dItem.style.cssText = 'padding:8px 10px;background:var(--card);border:1px solid var(--line);border-radius:6px;font-size:12px;';
     
     const top = el('div');
     top.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:8px;';
     const titleA = el('a', '', dr.title);
     titleA.style.cssText = 'font-weight:600;color:var(--fg);text-decoration:none;flex:1;';
     if (/^https?:\/\//.test(dr.url)) { titleA.href = dr.url; titleA.target = '_blank'; titleA.rel = 'noopener noreferrer'; }
     
     const sBadge = el('span', 'badge ' + (dr.direction === 'UP' ? 'up' : dr.direction === 'DOWN' ? 'down' : ''), `${dr.direction} (${(dr.pct > 0 ? '+' : '') + (dr.pct || 0).toFixed(2)}%)`);
     top.append(titleA, sBadge);
     dItem.append(top);

     const sub = el('div');
     sub.style.cssText = 'display:flex;gap:8px;align-items:center;color:var(--mut);font-size:11px;margin-top:4px;flex-wrap:wrap;';
     sub.append(el('span', '', dr.publisher || 'Unknown'));
     if (dr.score) sub.append(el('span', '', `Score: ${dr.score.toFixed(2)}`));
     if (dr.event_clue) {
      const lag = dr.event_lag_hours != null ? ` (${dr.event_lag_hours}h lag)` : '';
      sub.append(el('span', 'badge event-time', `🕒 Event: ${dr.event_clue}${lag}`));
     }
     dItem.append(sub);

     // Historical matches
     if (dr.matches && dr.matches.length) {
      const mBox = el('div');
      mBox.style.cssText = 'margin-top:6px;padding:6px 8px;background:rgba(0,0,0,0.03);border-radius:4px;font-size:11px;';
      mBox.append(el('div', '', 'Matching historical events:'));
      mBox.querySelector('div').style.fontWeight = '600';
      dr.matches.slice(0, 2).forEach(m => {
       const mRow = el('div');
       mRow.style.cssText = 'margin-top:3px;color:var(--mut);display:flex;justify-content:space-between;gap:8px;';
       mRow.append(el('span', '', `• ${m.title.slice(0, 60)}… (${m.date})`));
       const mPct = el('span', '', `${m.direction.toUpperCase()} ${m.pct > 0 ? '+' : ''}${m.pct}%`);
       mPct.style.color = m.direction === 'up' ? 'var(--up)' : m.direction === 'down' ? 'var(--dn)' : 'var(--fl)';
       mRow.append(mPct);
       mBox.append(mRow);
      });
      dItem.append(mBox);
     }
     dList.append(dItem);
    });
    details.append(dList);
    card.append(details);
   }
   box.append(card);
  }
 } catch (e) {
  $('#predict-results-view').replaceChildren(el('div', 'empty', 'Prediction error: ' + e.message));
 }
 $('#btn-run-predictions').disabled = false;
}

async function runVisualBacktest(){
 ANALYSIS_VIEW = 'backtest';
 $('#predict-results-view').style.display = 'none';
 $('#backtest-results-view').style.display = 'block';
 $('#backtest-results-view').replaceChildren(el('div', 'empty', '📊 Evaluating model historical accuracy and edge across markets…'));
 $('#btn-run-backtest').disabled = true;

 try {
  const m = $('#pmarket').value;
  const stats = await api(`/api/backtest_view?market=${m}`);
  const box = $('#backtest-results-view');
  box.replaceChildren();

  if (!stats || !stats.length) {
   box.append(el('div', 'empty', 'No backtest stats available. Run price labeling first.'));
   $('#btn-run-backtest').disabled = false;
   return;
  }

  for (const mkt of stats) {
   const card = el('div', 'mkt-card');
   const head = el('div');
   head.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;border-bottom:1px solid var(--line);padding-bottom:10px;flex-wrap:wrap;gap:8px;';
   
   const title = el('h3', '', `📊 ${mkt.name} (${mkt.symbol})`);
   title.style.cssText = 'margin:0;font-size:16px;';
   const info = el('span', 'badge', `${mkt.outcomes} Outcomes Tested · ${mkt.articles} Articles`);
   head.append(title, info);
   card.append(head);

   const hzKeys = ['1', '6', '24'].filter(k => mkt.horizons && mkt.horizons[k]);
   if (!hzKeys.length) {
    card.append(el('div', 'empty', 'No outcome records labeled for this market yet.'));
   } else {
    for (const k of hzKeys) {
     const hz = mkt.horizons[k];
     const hSec = el('div');
     hSec.style.cssText = 'margin-top:10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;';
     const hTitle = el('h4', '', `+${hz.horizon}h Forecast Accuracy & Edge Scorecard`);
     hTitle.style.cssText = 'margin:0 0 8px;font-size:13px;color:var(--fg);';
     hSec.append(hTitle);

     const kpiGrid = el('div', 'kpi-grid');
     
     // Accuracy
     const cAcc = el('div', 'kpi-card');
     const vAcc = el('div', 'kpi-val', hz.accuracy.toFixed(1) + '%'); vAcc.style.color = 'var(--pri)';
     cAcc.append(vAcc, el('div', 'kpi-lbl', 'Model Accuracy'));
     kpiGrid.append(cAcc);

     // Base rate
     const cBase = el('div', 'kpi-card');
     const vBase = el('div', 'kpi-val', hz.base_rate.toFixed(1) + '%'); vBase.style.color = 'var(--mut)';
     cBase.append(vBase, el('div', 'kpi-lbl', 'Baseline Rate'));
     kpiGrid.append(cBase);

     // Alpha / Edge
     const cEdge = el('div', 'kpi-card');
     const vEdge = el('div', 'kpi-val', (hz.edge >= 0 ? '+' : '') + hz.edge.toFixed(1) + '%');
     vEdge.style.color = hz.edge > 0 ? 'var(--up)' : hz.edge < 0 ? 'var(--dn)' : 'var(--mut)';
     cEdge.append(vEdge, el('div', 'kpi-lbl', 'Model Edge (Alpha)'));
     kpiGrid.append(cEdge);

     // Calls
     const cCalls = el('div', 'kpi-card');
     cCalls.append(el('div', 'kpi-val', String(hz.calls)), el('div', 'kpi-lbl', 'Active Calls'));
     kpiGrid.append(cCalls);

     // Strong Signal
     const cStrong = el('div', 'kpi-card');
     const vStrong = el('div', 'kpi-val', hz.strong_accuracy.toFixed(1) + '%'); vStrong.style.color = 'var(--pri)';
     cStrong.append(vStrong, el('div', 'kpi-lbl', `Strong Signals (${hz.strong_calls})`));
     kpiGrid.append(cStrong);

     // Headline Sentiment
     const cSent = el('div', 'kpi-card');
     const vSent = el('div', 'kpi-val', hz.sentiment_accuracy.toFixed(1) + '%');
     cSent.append(vSent, el('div', 'kpi-lbl', `Headline Sentiment (${hz.sentiment_calls})`));
     kpiGrid.append(cSent);

     hSec.append(kpiGrid);
     card.append(hSec);
    }
   }
   box.append(card);
  }
 } catch (e) {
  $('#backtest-results-view').replaceChildren(el('div', 'empty', 'Backtest error: ' + e.message));
 }
 $('#btn-run-backtest').disabled = false;
}

// Toggle Raw Terminal
let SHOW_RAW_TERMINAL = false;
$('#btn-toggle-raw').onclick = () => {
 SHOW_RAW_TERMINAL = !SHOW_RAW_TERMINAL;
 $('#out-analysis').style.display = SHOW_RAW_TERMINAL ? 'block' : 'none';
 $('#btn-toggle-raw').textContent = SHOW_RAW_TERMINAL ? 'Hide Terminal Log' : 'Terminal Log View';
};

$('#btn-run-predictions').onclick = runVisualPredictions;
$('#btn-run-backtest').onclick = runVisualBacktest;

// =============================================================
// DATABASE TAB (EXCEL / SPREADSHEET VIEW)
// =============================================================
let DB_PAGE = 1;
let DB_PAGE_SIZE = 25;
let DB_SORT_COL = 'ts';
let DB_SORT_DIR = 'desc';
let DB_TOTAL_PAGES = 1;
let DB_LOADED = false;
let dbSearchTimer = null;

async function loadDatabase(){
 const tbody = $('#db-tbody');
 tbody.replaceChildren();
 const loadingRow = el('tr');
 const td = el('td', '', 'Loading database rows…');
 td.colSpan = 12; td.style.textAlign = 'center'; td.style.padding = '24px'; td.style.color = 'var(--mut)';
 loadingRow.append(td);
 tbody.append(loadingRow);

 const q = $('#db-search').value.trim();
 const m = $('#db-market').value;
 const rel = $('#db-relonly').checked ? 1 : 0;
 const ev = $('#db-eventonly').checked ? 1 : 0;
 const body = $('#db-hasbody').checked ? 1 : 0;

 try {
  const url = `/api/database?page=${DB_PAGE}&page_size=${DB_PAGE_SIZE}&q=${encodeURIComponent(q)}&market=${m}&relevant=${rel}&event_only=${ev}&has_body=${body}&sort_col=${DB_SORT_COL}&sort_dir=${DB_SORT_DIR}`;
  const res = await api(url);
  DB_LOADED = true;
  DB_TOTAL_PAGES = res.total_pages || 1;
  DB_PAGE = res.page;

  $('#db-records-info').textContent = `Showing ${(res.page - 1) * res.page_size + 1}-${Math.min(res.page * res.page_size, res.total)} of ${res.total.toLocaleString()} articles`;
  $('#db-page-summary').textContent = `Page ${res.page} of ${DB_TOTAL_PAGES}`;
  $('#db-curr-page-num').textContent = String(res.page);
  $('#db-jump-page').max = DB_TOTAL_PAGES;
  $('#db-jump-page').value = res.page;

  $('#db-first-page').disabled = $('#db-prev-page').disabled = (res.page <= 1);
  $('#db-next-page').disabled = $('#db-last-page').disabled = (res.page >= DB_TOTAL_PAGES);

  // Update table header sorting icons
  $$('#db-table th[data-col]').forEach(th => {
   const col = th.dataset.col;
   const baseText = th.textContent.replace(/[ ⇕▲▼]+$/, '');
   if (col === DB_SORT_COL) {
    th.textContent = baseText + (DB_SORT_DIR === 'asc' ? ' ▲' : ' ▼');
    th.style.color = 'var(--pri)';
   } else {
    th.textContent = baseText + ' ⇕';
    th.style.color = '';
   }
  });

  $('#db-sort-info').textContent = `Sorted by: ${DB_SORT_COL} (${DB_SORT_DIR.toUpperCase()})`;

  tbody.replaceChildren();
  if (!res.rows || !res.rows.length) {
   const emptyRow = el('tr');
   const eTd = el('td', '', 'No articles match these search criteria.');
   eTd.colSpan = 12; eTd.style.textAlign = 'center'; eTd.style.padding = '24px'; eTd.style.color = 'var(--mut)';
   emptyRow.append(eTd);
   tbody.append(emptyRow);
   return;
  }

  res.rows.forEach(r => {
   const tr = el('tr');

   // 1. ID
   tr.append(el('td', '', String(r.id)));

   // 2. Date
   const dt = (r.published || '').replace('T', ' ').slice(0, 16);
   const tdDate = el('td', '', dt);
   tdDate.style.whiteSpace = 'nowrap';
   tr.append(tdDate);

   // 3. Publisher
   const tdPub = el('td', '', r.publisher || '');
   tdPub.style.whiteSpace = 'nowrap';
   tr.append(tdPub);

   // 4. Headline
   const tdTitle = el('td');
   const titleLink = el('a', '', r.title);
   titleLink.style.cssText = 'color:var(--fg);text-decoration:none;font-weight:500;';
   if (/^https?:\/\//.test(r.url)) {
    titleLink.href = r.url; titleLink.target = '_blank'; titleLink.rel = 'noopener noreferrer';
   }
   if (r.summary) {
    titleLink.title = r.summary;
   }
   tdTitle.append(titleLink);
   tr.append(tdTitle);

   // 5. Market
   const tdMkt = el('td');
   if (r.market_tag) {
    tdMkt.append(el('span', 'badge', r.market_tag.toUpperCase()));
   }
   tr.append(tdMkt);

   // 6. Score
   const tdScore = el('td');
   const sc = el('span', 'score', r.score.toFixed(2));
   sc.style.fontSize = '11px';
   tdScore.append(sc);
   tr.append(tdScore);

   // 7. Sentiment
   const tdSent = el('td');
   if (r.direction) {
    tdSent.append(el('span', 'badge ' + (r.direction === 'up' || r.direction === 'down' ? r.direction : ''), r.direction.toUpperCase()));
   }
   tr.append(tdSent);

   // 8. Event Clue
   const tdEv = el('td');
   if (r.event_clue) {
    const evLag = r.event_lag_hours != null ? ` (${r.event_lag_hours}h lag)` : '';
    const evBadge = el('span', 'badge event-time', `🕒 ${r.event_clue}${evLag}`);
    if (r.event_utc) evBadge.title = `Event at ${r.event_utc} UTC`;
    tdEv.append(evBadge);
   } else {
    tdEv.textContent = '—';
    tdEv.style.color = 'var(--mut)';
   }
   tr.append(tdEv);

   // 9, 10, 11: +1h, +6h, +24h Move
   for (const h of ['1', '6', '24']) {
    const tdOut = el('td');
    tdOut.style.whiteSpace = 'nowrap';
    const o = r.outcomes && r.outcomes[h];
    if (o) {
     const span = el('span', '', `${o.pct > 0 ? '+' : ''}${o.pct}%`);
     span.style.fontWeight = '600';
     span.style.color = o.dir === 'up' ? 'var(--up)' : o.dir === 'down' ? 'var(--dn)' : 'var(--fl)';
     tdOut.append(span);
    } else {
     tdOut.textContent = '—';
     tdOut.style.color = 'var(--mut)';
    }
    tr.append(tdOut);
   }

   // 12. Body
   const tdBody = el('td');
   tdBody.style.textAlign = 'center';
   if (r.body_status === 'ok' || r.body_status === 'has_body') {
    tdBody.textContent = '✓';
    tdBody.style.color = 'var(--up)';
    tdBody.style.fontWeight = '700';
   } else {
    tdBody.textContent = '—';
    tdBody.style.color = 'var(--mut)';
   }
   tr.append(tdBody);

   tbody.append(tr);
  });

 } catch (e) {
  tbody.replaceChildren();
  const errRow = el('tr');
  const tdErr = el('td', '', 'Database error: ' + e.message);
  tdErr.colSpan = 12; tdErr.style.textAlign = 'center'; tdErr.style.padding = '24px'; tdErr.style.color = 'var(--dn)';
  errRow.append(tdErr);
  tbody.append(errRow);
 }
}

// Table Header Click Sorting
$$('#db-table th[data-col]').forEach(th => {
 th.onclick = () => {
  const col = th.dataset.col;
  if (DB_SORT_COL === col) {
   DB_SORT_DIR = (DB_SORT_DIR === 'asc') ? 'desc' : 'asc';
  } else {
   DB_SORT_COL = col;
   DB_SORT_DIR = 'desc';
  }
  DB_PAGE = 1;
  loadDatabase();
 };
});

// Database Filters and Pagination Listeners
$('#db-search').oninput = () => {
 clearTimeout(dbSearchTimer);
 dbSearchTimer = setTimeout(() => { DB_PAGE = 1; loadDatabase(); }, 350);
};

$('#db-market').onchange = $('#db-relonly').onchange = $('#db-eventonly').onchange = $('#db-hasbody').onchange = () => {
 DB_PAGE = 1; loadDatabase();
};

$('#db-pagesize').onchange = () => {
 DB_PAGE = 1;
 DB_PAGE_SIZE = parseInt($('#db-pagesize').value);
 loadDatabase();
};

$('#db-reload').onclick = () => loadDatabase();

$('#db-first-page').onclick = () => { DB_PAGE = 1; loadDatabase(); };
$('#db-prev-page').onclick = () => { if (DB_PAGE > 1) { DB_PAGE--; loadDatabase(); } };
$('#db-next-page').onclick = () => { if (DB_PAGE < DB_TOTAL_PAGES) { DB_PAGE++; loadDatabase(); } };
$('#db-last-page').onclick = () => { DB_PAGE = DB_TOTAL_PAGES; loadDatabase(); };

$('#db-jump-page').onchange = (e) => {
 const p = parseInt(e.target.value);
 if (p >= 1 && p <= DB_TOTAL_PAGES) {
  DB_PAGE = p; loadDatabase();
 }
};

// Export CSV
async function exportDatabaseCsv(){
 const btn = $('#db-export-csv');
 const origText = btn.textContent;
 btn.textContent = '⏳ Exporting…'; btn.disabled = true;

 try {
  const q = $('#db-search').value.trim();
  const m = $('#db-market').value;
  const rel = $('#db-relonly').checked ? 1 : 0;
  const ev = $('#db-eventonly').checked ? 1 : 0;
  const body = $('#db-hasbody').checked ? 1 : 0;
  
  // Fetch up to 2000 matching rows
  const url = `/api/database?page=1&page_size=2000&q=${encodeURIComponent(q)}&market=${m}&relevant=${rel}&event_only=${ev}&has_body=${body}&sort_col=${DB_SORT_COL}&sort_dir=${DB_SORT_DIR}`;
  const res = await api(url);
  
  const headers = ['ID', 'Date_UTC', 'Publisher', 'Headline', 'Market', 'Score', 'Sentiment', 'Event_Clue', 'Event_Lag_Hours', 'Move_1h_pct', 'Move_6h_pct', 'Move_24h_pct', 'Has_Body', 'URL'];
  const rows = [headers.join(',')];

  (res.rows || []).forEach(r => {
   const row = [
    r.id,
    `"${(r.published || '').replace(/"/g, '""')}"`,
    `"${(r.publisher || '').replace(/"/g, '""')}"`,
    `"${(r.title || '').replace(/"/g, '""')}"`,
    `"${r.market_tag || ''}"`,
    r.score,
    `"${r.direction || ''}"`,
    `"${(r.event_clue || '').replace(/"/g, '""')}"`,
    r.event_lag_hours != null ? r.event_lag_hours : '',
    r.outcomes && r.outcomes['1'] ? r.outcomes['1'].pct : '',
    r.outcomes && r.outcomes['6'] ? r.outcomes['6'].pct : '',
    r.outcomes && r.outcomes['24'] ? r.outcomes['24'].pct : '',
    r.body_status === 'ok' || r.body_status === 'has_body' ? 'YES' : 'NO',
    `"${(r.url || '').replace(/"/g, '""')}"`
   ];
   rows.push(row.join(','));
  });

  const blob = new Blob(["\uFEFF" + rows.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `news_database_${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
 } catch (e) {
  alert('Export error: ' + e.message);
 }
 btn.textContent = origText; btn.disabled = false;
}

$('#db-export-csv').onclick = exportDatabaseCsv;


// Toggle Prediction Candles
$('#btn-toggle-pred').onclick=()=>{
 SHOW_PREDICTION=!SHOW_PREDICTION;
 const btn=$('#btn-toggle-pred');
 if(SHOW_PREDICTION){
  btn.classList.add('pred-on');
  btn.textContent='🔮 Hide Prediction Candles';
 } else {
  btn.classList.remove('pred-on');
  btn.textContent='🔮 Show Prediction Candles';
 }
 loadCandles();
};

// Event Listeners
$('#btn-full-sync').onclick=startFullSync;
$('#header-quick-sync').onclick=()=>{
 $$('#nav button').forEach(x=>x.classList.toggle('on',x.dataset.t==='data'));
 $$('.tab').forEach(t=>t.classList.toggle('on',t.id==='data'));
 startFullSync();
};

$$('[data-run]').forEach(b=>b.onclick=()=>{
 const cmd=b.dataset.run,inAnalysis=!!b.closest('#analysis');
 const args={};
 if(cmd==='predict'){ args.hours=$('#phours').value; args.market=$('#pmarket').value; }
 else if(cmd==='backtest'){ args.market=$('#pmarket').value; }
 run(cmd,args,inAnalysis?'out-analysis':'out-data');
});

$('#backfill').onclick=()=>{
 const s=$('#d0').value,e=$('#d1').value;
 if(!s){$('#out-data').textContent='Pick a start date.';return;}
 run('collect',{start:s,end:e||s},'out-data');
};

$('#hours').onchange=$('#mfilter').onchange=$('#reload').onclick=loadStories;
$('#msymbol').onchange=$('#mhours').onchange=$('#reload-candles').onclick=loadCandles;
$('#relonly').onchange=$('#q').oninput=$('#story-sort').onchange=$('#story-dir').onchange=$('#eventonly').onchange=renderStories;

// Initial Load
loadStats();
loadTicker();
loadStories();
loadCandles();
setInterval(loadTicker, 30000);
</script></body></html>
"""


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
