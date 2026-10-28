"""News -> price-move tracker. Standard library only, no machine learning.

  python News.py collect [--date YYYY-MM-DD | --from D --to D]   store news (today's feeds if no date)
  python News.py bodies  [--limit N] [--all]                      scrape article bodies
  python News.py rescore                                          recompute scores after editing weights/trust/topic
  python News.py top [--hours 24] [--n 15] [--show-merges]        best-scored stories (merged duplicates)
  python News.py label                                            price at news time + up/down after +1h/+6h/+24h
  python News.py predict [--hours 24]                             compare today's news with labeled history
  python News.py backtest [--horizon H]                           does the predictor beat the base rate?
  python News.py export [--show-merges]                           scored + labeled data -> output/news_labeled.csv
  python News.py update                                           collect + bodies + label + predict
  python News.py probe                                            show the raw price-candle response
  python News.py doctor                                           diagnose database row counts and pipeline status
  python News.py import-candles FILE.csv                          use your own price file instead of the API
  python News.py ui [--port 8765]                                 local web UI in your browser
  python News.py fake-prices                                      TEST ONLY: random prices stored as symbol 'FAKE'
"""
import argparse
import sys
from datetime import datetime

from analysis import backtest, doctor, export, predict, top
from common import load_config, open_db, rescore
from data import (collect, fake_prices, fetch_live_rates, import_candles, label, probe,
                  scrape_bodies)

def as_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--date")
    c.add_argument("--from", dest="start")
    c.add_argument("--to")
    b = sub.add_parser("bodies")
    b.add_argument("--limit", type=int, default=50)
    b.add_argument("--all", action="store_true", help="also irrelevant articles")
    lb = sub.add_parser("label")
    lb.add_argument("--market", choices=["all", "dollar", "gold", "btc"], default="all")
    sub.add_parser("rescore")
    sub.add_parser("fake-prices")
    sub.add_parser("prices", help="display live ticker rates for Dollar, Gold, and BTC")
    ic = sub.add_parser("import-candles")
    ic.add_argument("path")
    tp = sub.add_parser("top")
    tp.add_argument("--hours", type=float, default=24)
    tp.add_argument("--n", type=int, default=15)
    tp.add_argument("--show-merges", action="store_true")
    pr = sub.add_parser("predict")
    pr.add_argument("--hours", type=float, default=24)
    pr.add_argument("--market", choices=["all", "dollar", "gold", "btc"], default="all")
    bt = sub.add_parser("backtest")
    bt.add_argument("--horizon", type=int)
    bt.add_argument("--market", choices=["all", "dollar", "gold", "btc"], default="all")
    ex = sub.add_parser("export")
    ex.add_argument("--show-merges", action="store_true")
    ui = sub.add_parser("ui")
    ui.add_argument("--port", type=int, default=8765)
    for name in ("update", "probe", "doctor"):
        sub.add_parser(name)
    a = ap.parse_args()

    cfg = load_config()
    if a.cmd == "probe":
        return probe(cfg)
    if a.cmd == "ui":
        from ui import serve
        return serve(a.port)
    db = open_db()
    if a.cmd != "rescore" and db.execute("SELECT 1 FROM articles WHERE score IS NULL LIMIT 1").fetchone():
        rescore(db, cfg)  # first run after upgrading an older database
    if a.cmd == "collect":
        d0 = as_date(a.date or a.start) if (a.date or a.start) else None
        d1 = as_date(a.date or a.to or a.start) if d0 else None
        collect(db, cfg, d0, d1)
    elif a.cmd == "bodies":
        scrape_bodies(db, cfg, a.limit, a.all)
    elif a.cmd == "prices":
        r = fetch_live_rates()
        print("=== Live Multi-Market Ticker ===")
        if "dollar_free" in r:
            p, ch = r["dollar_free"]["rial"], r["dollar_free"]["change"]
            print(f"💵 Dollar Free Market: {p:,.0f} Rial ({p/10:,.0f} Toman)  [{ch:+.2f}%]")
        if "usdt" in r:
            p, ch = r["usdt"]["rial"], r["usdt"]["change"]
            print(f"💵 Tether (USDT):     {p:,.0f} Rial ({p/10:,.0f} Toman)  [{ch:+.2f}%]")
        if "gold_emami" in r:
            p, ch = r["gold_emami"]["rial"], r["gold_emami"]["change"]
            print(f"🥇 Gold Emami Coin:   {p:,.0f} Rial ({p/10:,.0f} Toman)  [{ch:+.2f}%]")
        if "gold_18k" in r:
            p, ch = r["gold_18k"]["rial"], r["gold_18k"]["change"]
            print(f"🥇 Gold 18K (gram):   {p:,.0f} Rial ({p/10:,.0f} Toman)  [{ch:+.2f}%]")
        if "paxg" in r:
            p, ch = r["paxg"]["rial"], r["paxg"]["change"]
            print(f"🥇 PAX Gold (Nobitex):{p:,.0f} Rial ({p/10:,.0f} Toman)  [{ch:+.2f}%]")
        if "btc_rls" in r:
            p, ch = r["btc_rls"]["rial"], r["btc_rls"]["change"]
            usd = r.get("btc_usdt", {}).get("usd", 0)
            print(f"₿  Bitcoin (Nobitex):  {p:,.0f} Rial (~{p/10000000000:,.2f}B Toman | ${usd:,.0f})  [{ch:+.2f}%]")
    elif a.cmd == "label":
        label(db, cfg, market_filter=getattr(a, "market", "all"))
    elif a.cmd == "rescore":
        rescore(db, cfg)
    elif a.cmd == "doctor":
        doctor(db, cfg)
    elif a.cmd == "fake-prices":
        fake_prices(db, cfg)
    elif a.cmd == "import-candles":
        import_candles(db, cfg, a.path)
    elif a.cmd == "top":
        top(db, cfg, a.hours, a.n, a.show_merges)
    elif a.cmd == "predict":
        predict(db, cfg, a.hours, market=getattr(a, "market", "all"))
    elif a.cmd == "backtest":
        backtest(db, cfg, a.horizon, market=getattr(a, "market", "all"))
    elif a.cmd == "export":
        export(db, cfg, a.show_merges)
    elif a.cmd == "update":
        collect(db, cfg)
        scrape_bodies(db, cfg, 40)
        label(db, cfg)
        top(db, cfg, 24, 15)
        print()
        predict(db, cfg, 24)


if __name__ == "__main__":
    main()