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

from analysis import annotate, backtest, doctor, export, predict, top
from common import all_rows, load_config, load_outcomes, open_db, rescore
from data import collect, label, scrape_bodies

LOCK = threading.Lock()  # one job at a time (jobs print to a shared, captured stdout)


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
        label(db, cfg)
    elif name == "rescore":
        rescore(db, cfg)
    elif name == "export":
        export(db, cfg)
    elif name == "predict":
        predict(db, cfg, float(a.get("hours", 24)))
    elif name == "backtest":
        backtest(db, cfg)
    elif name == "doctor":
        doctor(db, cfg)
    elif name == "update":
        collect(db, cfg)
        scrape_bodies(db, cfg, 40)
        label(db, cfg)
        top(db, cfg, 24, 15)
        print()
        predict(db, cfg, 24)
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
    m = cfg["market"]
    return {"articles": one("SELECT COUNT(*) FROM articles"),
            "relevant": one("SELECT COUNT(*) FROM articles WHERE relevant=1"),
            "bodies": one("SELECT COUNT(*) FROM articles WHERE body_status='ok'"),
            "outcomes": one("SELECT COUNT(*) FROM outcomes"),
            "candles": one("SELECT COUNT(*) FROM candles WHERE symbol=? AND res=?",
                           m["symbol"], m["resolution"]),
            "symbol": m["symbol"], "horizons": cfg["horizons_hours"]}


def api_top(q):
    hours = float(q.get("hours", ["24"])[0])
    n = int(q.get("n", ["100"])[0])
    cfg, db = load_config(), open_db()
    now = time.time()
    rows = [r for r in all_rows(db, cfg) if hours <= 0 or r["ts"] >= now - hours * 3600]
    if not rows:
        return []
    stories, _ = annotate(rows, cfg)
    outs = load_outcomes(db)
    res = []
    for st in sorted(stories, key=lambda s: -s["score"])[:n]:
        r = st["best"]
        res.append({"score": st["score"], "repeat": st["repeat"], "sources": st["repeat_sources"],
                    "relevant": bool(r["relevant"]), "direction": r["direction"] or "",
                    "publisher": r["publisher"], "title": r["title"], "url": r["url"],
                    "published": r["published_utc"], "also": st["also"],
                    "out": {str(h): {"pct": round(outs[(r["id"], h)][0], 2),
                                     "dir": outs[(r["id"], h)][1]}
                            for h in cfg["horizons_hours"] if (r["id"], h) in outs}})
    return res


def api_candles(q):
    hours = float(q.get("hours", ["72"])[0])
    cfg, db = load_config(), open_db()
    m = cfg["market"]
    sym, res = m["symbol"], m["resolution"]
    now = time.time()
    t_min = (now - hours * 3600) if hours > 0 else 0
    rows = db.execute(
        "SELECT ts, o, c FROM candles WHERE symbol=? AND res=? AND ts >= ? ORDER BY ts ASC",
        (sym, res, int(t_min))
    ).fetchall()
    return [{"ts": r["ts"], "open": r["o"], "close": r["c"]} for r in rows]


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

    def _host_ok(self):  # blocks DNS-rebinding style access from other sites
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
            if u.path == "/api/top":
                return self._send(200, json.dumps(api_top(parse_qs(u.query))))
            if u.path == "/api/candles":
                return self._send(200, json.dumps(api_candles(parse_qs(u.query))))
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
        raise SystemExit(f"Cannot start the UI on port {port}: {e}\n"
                         f"Is it already running? Try another port:  python News.py ui --port {port + 1}")
    url = f"http://127.0.0.1:{port}/"
    print(f"News UI running at {url}", flush=True)
    print("Open that address in your browser if it does not open by itself. Ctrl+C stops it.",
          flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News → price tracker</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1b19;--mut:#6b6a66;--card:#fff;--line:#e4e2dc;--acc:#2b5fd9;
--up:#13734a;--upbg:#e0f3e9;--dn:#a3262a;--dnbg:#fbe4e4;--fl:#555;--flbg:#ebebe8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--mut:#9a988f;--card:#1f1f1c;
--line:#33332f;--acc:#7ea2ff;--up:#6fd9a3;--upbg:#173a2a;--dn:#ff9a9d;--dnbg:#43201f;--fl:#bbb;--flbg:#2c2c29}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{padding:16px 20px 0;max-width:1100px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}
#stats{color:var(--mut);font-size:13px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);max-width:1100px;margin:12px auto 0;padding:0 20px}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;color:var(--mut);
font:inherit;cursor:pointer}
nav button.on{color:var(--fg);border-color:var(--acc);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:16px 20px 60px}
.tab{display:none}.tab.on{display:block}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
select,input,button.act{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}
button.act{cursor:pointer}button.act:hover{border-color:var(--acc)}
button.act.main{background:var(--acc);border-color:var(--acc);color:#fff}
button:disabled{opacity:.5;cursor:wait}
.story{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin-bottom:8px}
.story .t{font-weight:600;color:var(--fg);text-decoration:none}.story .t:hover{color:var(--acc)}
.meta{color:var(--mut);font-size:13px;margin-top:3px;display:flex;flex-wrap:wrap;gap:4px 12px}
.score{display:inline-block;min-width:42px;text-align:center;font-weight:700;border-radius:5px;
padding:1px 6px;background:var(--flbg);margin-right:8px;font-variant-numeric:tabular-nums}
.badge{font-size:12px;border-radius:4px;padding:1px 6px;background:var(--flbg);color:var(--fl)}
.badge.up{background:var(--upbg);color:var(--up)}.badge.down{background:var(--dnbg);color:var(--dn)}
.outs{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;
white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,Consolas,monospace;min-height:80px}
.empty{color:var(--mut);padding:30px 0;text-align:center}
.grp{margin-bottom:18px}.grp h3{font-size:14px;margin:0 0 6px}
.hint{color:var(--mut);font-size:13px;margin:4px 0 10px}
</style></head><body>
<header><h1>News → price tracker</h1><div id="stats">loading…</div></header>
<nav id="nav">
<button data-t="stories" class="on">Stories</button>
<button data-t="market">Market &amp; Prices</button>
<button data-t="analysis">Predict &amp; backtest</button>
<button data-t="data">Update data</button>
</nav>
<main>
<section id="stories" class="tab on">
 <div class="bar">
  <label>Period <select id="hours"><option value="6">6 h</option><option value="24" selected>24 h</option>
   <option value="72">3 days</option><option value="168">7 days</option><option value="0">all</option></select></label>
  <label><input type="checkbox" id="relonly"> relevant only</label>
  <input id="q" placeholder="filter by text…" style="flex:1;min-width:160px">
  <button class="act" id="reload">Refresh</button>
 </div>
 <div id="list"></div>
</section>
<section id="market" class="tab">
 <div class="bar">
  <label>Candle Window <select id="mhours">
   <option value="24">24 h</option>
   <option value="72" selected>3 days</option>
   <option value="168">7 days</option>
   <option value="0">All</option>
  </select></label>
  <button class="act" id="reload-candles">Refresh Prices</button>
 </div>
 <div id="market-stats" class="hint">Loading candles…</div>
 <div class="story" style="overflow-x:auto;">
  <svg id="candle-chart" width="1000" height="260" style="display:block;width:100%;"></svg>
 </div>
 <div id="candle-table-box" style="margin-top:14px;max-height:350px;overflow-y:auto;"></div>
</section>
<section id="analysis" class="tab">
 <div class="bar">
  <label>News from the last <select id="phours"><option value="6">6 h</option><option value="24" selected>24 h</option>
   <option value="72">3 days</option></select></label>
  <button class="act main" data-run="predict">Predict</button>
  <button class="act" data-run="backtest">Backtest</button>
 </div>
 <div class="hint">Backtest shows whether predictions beat the always-guess-majority baseline. Trust predictions only if it clearly does.</div>
 <pre id="out-analysis">Press a button.</pre>
</section>
<section id="data" class="tab">
 <div class="grp"><h3>Today's feeds</h3><div class="bar">
  <button class="act main" data-run="update">Update everything</button>
  <button class="act" data-run="collect">Collect only</button></div></div>
 <div class="grp"><h3>Backfill a date range</h3><div class="bar">
  <input type="date" id="d0"> to <input type="date" id="d1">
  <button class="act" id="backfill">Collect range</button></div></div>
 <div class="grp"><h3>Other steps</h3><div class="bar">
  <button class="act" data-run="bodies">Scrape bodies</button>
  <button class="act" data-run="label">Label with prices</button>
  <button class="act" data-run="rescore">Rescore</button>
  <button class="act" data-run="doctor">Doctor</button>
  <button class="act" data-run="export">Export CSV</button></div></div>
 <pre id="out-data">Output appears here.</pre>
</section>
</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let STORIES=[], HORIZONS=[];
async function api(path,opts){const r=await fetch(path,opts);const t=await r.text();
 if(!r.ok)throw new Error(t||r.statusText);return JSON.parse(t);}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;
 if(text!==undefined)e.textContent=text;return e;}

$$('#nav button').forEach(b=>b.onclick=()=>{
 $$('#nav button').forEach(x=>x.classList.toggle('on',x===b));
 $$('.tab').forEach(t=>t.classList.toggle('on',t.id===b.dataset.t));
 if(b.dataset.t==='market') loadCandles();
});

async function loadStats(){
 try{const s=await api('/api/stats');HORIZONS=s.horizons;
  $('#stats').textContent=`${s.articles} articles · ${s.relevant} relevant · ${s.bodies} with body · `+
   `${s.outcomes} labeled outcomes · ${s.candles} ${s.symbol} candles`;
 }catch(e){$('#stats').textContent='Error: '+e.message;}}

function render(){
 const box=$('#list'),q=$('#q').value.trim().toLowerCase(),rel=$('#relonly').checked;
 box.replaceChildren();
 const items=STORIES.filter(s=>(!rel||s.relevant)&&(!q||(s.title+' '+s.publisher).toLowerCase().includes(q)));
 if(!items.length){box.append(el('div','empty','No stories for this filter. Try "Update data" first.'));return;}
 for(const s of items){
  const card=el('div','story'),line=el('div');
  line.append(el('span','score',s.score.toFixed(2)));
  const a=el('a','t',s.title);
  if(/^https?:\/\//.test(s.url)){a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';}
  line.append(a);card.append(line);
  const m=el('div','meta');
  m.append(el('span','',s.publisher));
  if(s.published)m.append(el('span','',s.published.replace('T',' ').slice(0,16)+' UTC'));
  if(s.repeat>1)m.append(el('span','',`merged ×${s.repeat} (${s.sources} outlet${s.sources>1?'s':''})`));
  m.append(el('span','badge',s.relevant?'relevant':'not relevant'));
  if(s.direction)m.append(el('span','badge '+(s.direction==='up'||s.direction==='down'?s.direction:''),'headline: '+s.direction));
  card.append(m);
  if(s.also)card.append(el('div','meta','Also: '+s.also));
  const keys=Object.keys(s.out);
  if(keys.length){const o=el('div','outs');
   for(const h of keys){const v=s.out[h];
    o.append(el('span','badge '+v.dir,`+${h}h: ${v.dir} ${v.pct>0?'+':''}${v.pct}%`));}
   card.append(o);}
  box.append(card);}}

async function loadStories(){
 $('#list').replaceChildren(el('div','empty','Loading…'));
 try{STORIES=await api('/api/top?hours='+$('#hours').value);render();}
 catch(e){$('#list').replaceChildren(el('div','empty','Error: '+e.message));}}

async function loadCandles(){
 const h=$('#mhours').value;
 $('#market-stats').textContent='Loading candles…';
 try{
  const data=await api('/api/candles?hours='+h);
  if(!data.length){
   $('#market-stats').textContent='No price candles stored for this timeframe.';
   $('#candle-chart').replaceChildren();
   $('#candle-table-box').replaceChildren();
   return;
  }
  const first=data[0], last=data[data.length-1];
  const pct=(((last.close-first.open)/first.open)*100).toFixed(2);
  $('#market-stats').textContent=
   `Showing ${data.length} candles | Latest: ${last.close.toLocaleString()} | Window Change: ${pct>0?'+':''}${pct}%`;
  renderChart(data);
  renderTable(data);
 }catch(e){
  $('#market-stats').textContent='Error loading candles: '+e.message;
 }
}

function renderChart(data){
 const svg=$('#candle-chart');
 svg.replaceChildren();
 const w=svg.clientWidth||1000, h=260, pad=30;
 const minP=Math.min(...data.map(d=>Math.min(d.open,d.close)));
 const maxP=Math.max(...data.map(d=>Math.max(d.open,d.close)))||1;
 const range=(maxP-minP)||1;

 const getY=p=>h-pad-((p-minP)/range)*(h-2*pad);
 const step=(w-2*pad)/data.length;

 const line=document.createElementNS('http://www.w3.org/2000/svg','line');
 line.setAttribute('x1',pad); line.setAttribute('y1',h-pad);
 line.setAttribute('x2',w-pad); line.setAttribute('y2',h-pad);
 line.setAttribute('stroke','var(--line)');
 svg.appendChild(line);

 data.forEach((d,idx)=>{
  const x=pad+idx*step;
  const yO=getY(d.open), yC=getY(d.close);
  const isUp=d.close>=d.open;
  const color=isUp?'var(--up)':'var(--dn)';

  const bar=document.createElementNS('http://www.w3.org/2000/svg','rect');
  bar.setAttribute('x',x);
  bar.setAttribute('y',Math.min(yO,yC));
  bar.setAttribute('width',Math.max(1,step-2));
  bar.setAttribute('height',Math.max(2,Math.abs(yC-yO)));
  bar.setAttribute('fill',color);
  svg.appendChild(bar);
 });
}

function renderTable(data){
 const box=$('#candle-table-box');
 const rows=[...data].reverse().slice(0,50);
 let html='<table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">'+
  '<thead><tr style="border-bottom:1px solid var(--line);color:var(--mut);">'+
  '<th style="padding:6px">UTC Date</th><th>Open</th><th>Close</th><th>Delta</th></tr></thead><tbody>';
 for(const r of rows){
  const d=new Date(r.ts*1000).toISOString().replace('T',' ').slice(0,16);
  const delta=(((r.close-r.open)/r.open)*100).toFixed(2);
  const clr=delta>0?'var(--up)':(delta<0?'var(--dn)':'var(--fl)');
  html+=`<tr style="border-bottom:1px solid var(--line);">
   <td style="padding:6px">${d}</td>
   <td>${r.open.toLocaleString()}</td>
   <td>${r.close.toLocaleString()}</td>
   <td style="color:${clr}">${delta>0?'+':''}${delta}%</td>
  </tr>`;
 }
 html+='</tbody></table>';
 box.innerHTML=html;
}

async function run(cmd,args,outId){
 const out=$('#'+outId),btns=$$('button.act');
 btns.forEach(b=>b.disabled=true);out.textContent='Running '+cmd+'… (can take a while)';
 try{const r=await api('/api/run',{method:'POST',
   headers:{'Content-Type':'application/json','X-UI':'1'},body:JSON.stringify({cmd,args})});
  out.textContent=r.output;}
 catch(e){out.textContent='Error: '+e.message;}
 btns.forEach(b=>b.disabled=false);loadStats();loadStories();loadCandles();}

$$('[data-run]').forEach(b=>b.onclick=()=>{
 const cmd=b.dataset.run,inAnalysis=!!b.closest('#analysis');
 run(cmd,cmd==='predict'?{hours:$('#phours').value}:{},inAnalysis?'out-analysis':'out-data');});
$('#backfill').onclick=()=>{const s=$('#d0').value,e=$('#d1').value;
 if(!s){$('#out-data').textContent='Pick a start date.';return;}
 run('collect',{start:s,end:e||s},'out-data');};
$('#hours').onchange=$('#reload').onclick=loadStories;
$('#mhours').onchange=$('#reload-candles').onclick=loadCandles;
$('#relonly').onchange=$('#q').oninput=render;

loadStats();loadStories();loadCandles();
</script></body></html>
"""


if __name__ == "__main__":  # `python ui.py [port]` works too
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)