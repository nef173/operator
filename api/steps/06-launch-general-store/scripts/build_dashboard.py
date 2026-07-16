#!/usr/bin/env python3
"""
build_dashboard.py — render the Competitor Best-Seller Spy as a self-contained
HTML dashboard (Store-Spy-style), reading local snapshots/ + movers.json +
store_traffic.json (TrendTrack enrichment).

Data is INLINED into the HTML (no fetch / no server / no CORS) so it opens
straight from file://. Re-run after each snapshot/diff to refresh.

Usage:
  build_dashboard.py --snapshots snapshots --movers movers.json \
                     --traffic store_traffic.json --out dashboard.html
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys


def latest_snapshot(store_dir):
    files = sorted(glob.glob(os.path.join(store_dir, "*.json")))
    return files[-1] if files else None


def tracked_roster():
    """Domains currently in stores.txt — the dashboard shows ONLY these, so a
    dropped store's leftover snapshot folder never leaks back into the view."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.txt")
    roster = set()
    if os.path.isfile(path):
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                roster.add(ln.lower())
    return roster


def collect(snap_dir, movers_path, traffic_path):
    traffic = {}
    if traffic_path and os.path.isfile(traffic_path):
        with open(traffic_path) as fh:
            traffic = json.load(fh).get("stores", {})
    roster = tracked_roster()
    stores = []
    for d in sorted(glob.glob(os.path.join(snap_dir, "*"))):
        if not os.path.isdir(d):
            continue
        if roster and os.path.basename(d.rstrip("/")).lower() not in roster:
            continue  # dropped store — skip its leftover snapshots
        f = latest_snapshot(d)
        if not f:
            continue
        with open(f) as fh:
            snap = json.load(fh)
        ref = dt.date.fromisoformat(snap["date"])
        for p in snap["products"]:
            try:
                p["days_old"] = (ref - dt.datetime.fromisoformat(
                    p["created_at"]).date()).days
            except (ValueError, KeyError, TypeError):
                p["days_old"] = None
            p["is_fresh"] = p["days_old"] is not None and p["days_old"] <= 45
        snap["traffic_meta"] = traffic.get(snap["store"])
        stores.append(snap)
    movers = None
    if movers_path and os.path.isfile(movers_path):
        with open(movers_path) as fh:
            movers = json.load(fh)
    return stores, movers


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Competitor Best-Seller Spy</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--ink:#e6e9ef;--mut:#8b93a7;
        --acc:#5b9dff;--new:#36d399;--gain:#5b9dff;--fall:#f87272;--fresh:#ffb020}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{padding:16px 24px;border-bottom:1px solid var(--line)}
  header h1{font-size:18px;margin:0 0 8px}
  .stats{display:flex;gap:22px;flex-wrap:wrap;color:var(--mut);font-size:12px}
  .stats b{color:var(--ink);font-size:15px;font-variant-numeric:tabular-nums}
  .stat{display:flex;flex-direction:column}
  .tabs{display:flex;gap:8px;padding:12px 24px 0}
  .tab{padding:8px 14px;border:1px solid var(--line);border-bottom:none;
       border-radius:8px 8px 0 0;cursor:pointer;color:var(--mut);background:var(--card)}
  .tab.active{color:var(--ink);border-color:var(--acc)}
  .wrap{padding:16px 24px}
  .controls{display:flex;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
  .controls input[type=search]{background:var(--card);border:1px solid var(--line);
     color:var(--ink);padding:7px 11px;border-radius:8px;width:260px;font-size:13px}
  .controls label{color:var(--mut);font-size:12px;display:flex;gap:6px;align-items:center;cursor:pointer}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
  .chip{padding:5px 10px;border:1px solid var(--line);border-radius:999px;
        cursor:pointer;color:var(--mut);font-size:12px;background:var(--card)}
  .chip.active{color:#fff;border-color:var(--acc);background:#1d2533}
  .store{margin-bottom:26px}
  .storehead{display:flex;align-items:center;gap:14px;margin:0 0 6px;flex-wrap:wrap}
  .storehead a.dom{color:var(--acc);text-decoration:none;font-size:15px;font-weight:600}
  .tmeta{display:flex;gap:14px;align-items:center;color:var(--mut);font-size:12px;flex-wrap:wrap}
  .tmeta .v{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
  .pos{color:var(--new)} .neg{color:var(--fall)}
  .pill{padding:1px 7px;border:1px solid var(--line);border-radius:999px}
  .pill.us-hi{color:var(--new);border-color:rgba(54,211,153,.4)}
  .pill.us-lo{color:var(--fall);border-color:rgba(248,114,114,.4)}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  tr:last-child td{border-bottom:none}
  td.rank{color:var(--mut);width:34px;text-align:right;font-variant-numeric:tabular-nums}
  img.thumb{width:42px;height:42px;object-fit:cover;border-radius:6px;background:#222;display:block}
  .title{font-weight:500}
  .title a{color:var(--ink);text-decoration:none}
  .title a:hover{color:var(--acc)}
  .age{color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
  .fresh{color:var(--fresh);font-weight:700}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
  .b-new{background:rgba(54,211,153,.15);color:var(--new)}
  .b-gain{background:rgba(91,157,255,.15);color:var(--gain)}
  .b-fall{background:rgba(248,114,114,.15);color:var(--fall)}
  .delta{font-variant-numeric:tabular-nums}
  .price{color:var(--mut);white-space:nowrap}
  .empty{color:var(--mut);padding:30px;text-align:center;border:1px dashed var(--line);border-radius:10px}
  .spk{vertical-align:middle}
  .spotlight{background:linear-gradient(180deg,#19222e,#171a21);border:1px solid #2a3a52;
             border-radius:12px;padding:14px 16px;margin-bottom:20px}
  .spotlight h3{margin:0 0 10px;font-size:13px;color:var(--fresh);letter-spacing:.03em}
  .cards{display:flex;gap:12px;overflow-x:auto;padding-bottom:4px}
  .pcard{min-width:180px;max-width:180px;background:var(--card);border:1px solid var(--line);
         border-radius:10px;padding:8px}
  .pcard img{width:100%;height:120px;object-fit:cover;border-radius:7px;background:#222}
  .pcard .t{font-size:12px;margin:6px 0 3px;line-height:1.3;height:32px;overflow:hidden}
  .pcard .s{font-size:11px;color:var(--mut)}
  .tierhead{margin:22px 0 8px;font-size:12px;font-weight:700;letter-spacing:.05em;
            text-transform:uppercase;color:var(--fresh);display:flex;align-items:center;gap:9px}
  .tierhead:first-child{margin-top:4px}
  .tierhead .tcount{color:var(--ink);background:var(--card);border:1px solid var(--line);
                    border-radius:999px;padding:1px 9px;font-size:11px;font-weight:600}
  #stores table{margin-bottom:6px}
  .momrule{margin:4px 0 16px;padding:10px 14px;background:var(--card);border:1px solid var(--line);
           border-left:3px solid var(--fall);border-radius:8px;font-size:12.5px;line-height:1.5;color:var(--ink)}
  .dropflag{color:var(--fall);font-weight:700;font-size:11px;white-space:nowrap}
</style></head><body>
<header>
  <h1>🕵️ Competitor Best-Seller Spy</h1>
  <div class="stats" id="stats"></div>
</header>
<div class="tabs">
  <div class="tab active" data-tab="best">Best Sellers</div>
  <div class="tab" data-tab="movers">Movers</div>
  <div class="tab" data-tab="stores">Stores</div>
</div>
<div class="wrap">
  <div class="controls">
    <input type="search" id="q" placeholder="Search products across all stores…">
    <label><input type="checkbox" id="freshOnly"> 🔥 fresh only (≤45d)</label>
  </div>
  <div class="chips" id="chips"></div>
  <div id="best"></div>
  <div id="movers" style="display:none"></div>
  <div id="stores" style="display:none"></div>
</div>
<script>
const DATA = __DATA__;
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const age = d => d==null?'—':(d<=45?'🔥':'')+d+'d';
const fmt = n => n==null?'—':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(0)+'k':String(n);
let activeStore='ALL', q='', freshOnly=false;

const TIERS=[
  {key:3,label:'TIER 3 · 350K–1M+ visits/mo',min:350000},
  {key:2,label:'TIER 2 · 100K–350K visits/mo',min:100000},
  {key:1,label:'TIER 1 · 30K–100K visits/mo',min:30000},
  {key:0,label:'Below 30K visits/mo',min:0}];
const tierOf = v => (v>=350000?3:v>=100000?2:v>=30000?1:0);
function mom(h){ // month-over-month % from history (last vs prev non-zero-ish)
  if(!h||h.length<2) return null;
  const a=h[h.length-2], b=h[h.length-1];
  if(!a) return null;
  return Math.round((b-a)/a*100);
}
function sparkline(h,w=84,ht=22){
  if(!h||!h.length) return '';
  const max=Math.max(...h,1), min=Math.min(...h);
  const rng=(max-min)||1;
  const pts=h.map((v,i)=>`${(i/(h.length-1)*w).toFixed(1)},${(ht-((v-min)/rng)*ht).toFixed(1)}`).join(' ');
  const up=h[h.length-1]>=h[0];
  return `<svg class="spk" width="${w}" height="${ht}"><polyline fill="none" stroke="${up?'#36d399':'#f87272'}" stroke-width="1.5" points="${pts}"/></svg>`;
}
function trafficLine(s){
  const t=s.traffic_meta; if(!t) return '<span class="tmeta">no TrendTrack data</span>';
  const g=mom(t.history);
  const usc=t.us_share>=0.7?'us-hi':t.us_share<0.4?'us-lo':'';
  return `<span class="tmeta">
    <span>📈 <span class="v">${fmt(t.monthly_visits)}</span>/mo</span>
    ${g==null?'':`<span class="${g>=0?'pos':'neg'}">${g>=0?'▲':'▼'} ${Math.abs(g)}% MoM</span>`}
    ${sparkline(t.history)}
    <span class="pill ${usc}">${Math.round((t.us_share||0)*100)}% US</span>
    <span>since ${esc(t.created||'?')}</span>
    ${t.active_meta_ads?`<span class="pill">${t.active_meta_ads} Meta ads</span>`:''}
  </span>`;
}
function stats(){
  const n=DATA.stores.length;
  const date=DATA.stores[0]?.date||'—';
  const prod=DATA.stores.reduce((a,s)=>a+s.count,0);
  const visits=DATA.stores.reduce((a,s)=>a+(s.traffic_meta?.monthly_visits||0),0);
  const fresh=DATA.stores.reduce((a,s)=>a+s.products.filter(p=>p.is_fresh).length,0);
  const mv=DATA.movers?`${DATA.movers.totals.new||0} new · ${DATA.movers.totals.gainers||0} gainers`:'need day 2';
  document.getElementById('stats').innerHTML=
    `<div class="stat"><b>${n}</b>stores</div>
     <div class="stat"><b>${prod}</b>products tracked</div>
     <div class="stat"><b>${fmt(visits)}</b>combined visits/mo</div>
     <div class="stat"><b>${fresh}</b>fresh (≤45d)</div>
     <div class="stat"><b>${mv}</b>movers</div>
     <div class="stat"><b>${date}</b>snapshot</div>`;
}
function chips(){
  const c=document.getElementById('chips');
  const stores=['ALL',...DATA.stores.map(s=>s.store)];
  c.innerHTML=stores.map(s=>`<div class="chip ${s===activeStore?'active':''}" data-s="${esc(s)}">${esc(s)}</div>`).join('');
  c.querySelectorAll('.chip').forEach(el=>el.onclick=()=>{activeStore=el.dataset.s;chips();render();});
}
function match(p){
  if(freshOnly && !p.is_fresh) return false;
  if(q && !(p.title||'').toLowerCase().includes(q)) return false;
  return true;
}
function row(p,store){
  const img=p.image?`<img class="thumb" src="${esc(p.image)}" loading="lazy">`:'<div class="thumb"></div>';
  const price=p.price!=null?`${p.price} ${esc(store.base_currency||'')}`:'';
  return `<tr><td class="rank">${p.rank}</td><td>${img}</td>
    <td class="title"><a href="${esc(p.url)}" target="_blank">${esc(p.title)}</a></td>
    <td class="price">${price}</td>
    <td class="age ${p.is_fresh?'fresh':''}">${age(p.days_old)}</td></tr>`;
}
function spotlight(){
  // fresh winners across all stores (created <=45d), best rank first
  let fresh=[];
  DATA.stores.forEach(s=>s.products.filter(p=>p.is_fresh).forEach(p=>fresh.push({...p,store:s.store})));
  fresh.sort((a,b)=>(a.days_old-b.days_old)||(a.rank-b.rank));
  if(!fresh.length) return '';
  return `<div class="spotlight"><h3>🔥 FRESH WINNERS — newly launched (≤45d) already ranking</h3>
    <div class="cards">${fresh.slice(0,16).map(p=>`<div class="pcard">
      ${p.image?`<img src="${esc(p.image)}" loading="lazy">`:'<div class="thumb"></div>'}
      <div class="t"><a href="${esc(p.url)}" target="_blank" style="color:inherit;text-decoration:none">${esc(p.title)}</a></div>
      <div class="s">${esc(p.store)} · #${p.rank} · ${p.days_old}d</div></div>`).join('')}</div></div>`;
}
function renderBest(){
  const host=document.getElementById('best');
  const stores=DATA.stores.filter(s=>activeStore==='ALL'||s.store===activeStore);
  let html = (activeStore==='ALL'?spotlight():'');
  html += stores.map(s=>{
    const rows=s.products.filter(match);
    if(!rows.length) return '';
    return `<div class="store"><div class="storehead">
        <a class="dom" href="https://${esc(s.store)}" target="_blank">${esc(s.store)}</a>
        ${trafficLine(s)}</div>
      <table><thead><tr><th>#</th><th></th><th>Product</th><th>Price</th><th>Age</th></tr></thead>
      <tbody>${rows.map(p=>row(p,s)).join('')}</tbody></table></div>`;
  }).join('');
  host.innerHTML = html || '<div class="empty">No products match the filter.</div>';
}
function renderMovers(){
  const host=document.getElementById('movers');
  if(!DATA.movers){host.innerHTML='<div class="empty">No movers yet — the diff needs ≥2 days of snapshots. First movers appear after the next day\u2019s run.</div>';return;}
  const stores=DATA.movers.stores.filter(s=>activeStore==='ALL'||s.store===activeStore);
  if(!stores.length){host.innerHTML='<div class="empty">No movers for this store.</div>';return;}
  const badge=c=>c==='new'?'<span class="badge b-new">NEW</span>':c==='gainer'?'<span class="badge b-gain">▲ GAINER</span>':c==='faller'?'<span class="badge b-fall">▼ FALLER</span>':c;
  const delta=m=>m.rank_delta==null?'—':`<span class="delta ${m.rank_delta>0?'pos':'neg'}">${m.rank_delta>0?'+':''}${m.rank_delta}</span>`;
  host.innerHTML=stores.map(s=>`<div class="store"><div class="storehead">
      <a class="dom" href="https://${esc(s.store)}" target="_blank">${esc(s.store)}</a>
      <span class="tmeta">${esc(s.prior_date)} → ${esc(s.latest_date)}</span></div>
    <table><thead><tr><th></th><th></th><th>Product</th><th>Rank</th><th>Δ</th><th>Age</th></tr></thead>
    <tbody>${s.movers.filter(match).map(m=>`<tr><td>${badge(m.class)}</td>
      <td>${m.image?`<img class="thumb" src="${esc(m.image)}" loading="lazy">`:'<div class="thumb"></div>'}</td>
      <td class="title"><a href="${esc(m.url)}" target="_blank">${esc(m.title)}</a></td>
      <td class="rank">${m.prior_rank==null?'—':m.prior_rank}→${m.rank}</td>
      <td>${delta(m)}</td>
      <td class="age ${m.is_fresh?'fresh':''}">${age(m.days_old)}</td></tr>`).join('')}</tbody></table></div>`).join('');
}
function renderStores(){
  const host=document.getElementById('stores');
  const rows=DATA.stores.map(s=>{const t=s.traffic_meta||{};const g=mom(t.history);
    return {store:s.store,v:t.monthly_visits||0,g,us:t.us_share,cat:t.category,created:t.created,prod:s.count,ads:t.active_meta_ads,h:t.history};})
    .sort((a,b)=>b.v-a.v);
  const rowHtml=r=>`<tr>
      <td class="title"><a href="https://${esc(r.store)}" target="_blank">${esc(r.store)}</a></td>
      <td class="price" style="color:var(--ink)">${fmt(r.v)}</td>
      <td>${r.g==null?'—':`<span class="${r.g>=0?'pos':'neg'}">${r.g>=0?'▲':'▼'} ${Math.abs(r.g)}%</span>${r.g<=-30?' <span class="dropflag" title="≤ −30% MoM — remove candidate (unless keep-exception)">⚠ remove?</span>':''}`}</td>
      <td>${sparkline(r.h)}</td>
      <td><span class="pill ${r.us>=0.7?'us-hi':r.us<0.4?'us-lo':''}">${r.us==null?'?':Math.round(r.us*100)+'%'}</span></td>
      <td class="age">${esc(r.created||'?')}</td>
      <td class="age">${r.ads||0}</td></tr>`;
  const drops=rows.filter(r=>r.g!=null&&r.g<=-30).length;
  const note=`<div class="momrule">📉 <b>Roster rule:</b> a store down <b>≤ −30% MoM</b> (last two months) is a <b>remove candidate</b> — flagged <span class="dropflag">⚠ remove?</span> below — unless it's a manual keep-exception (e.g. bowlift). ${drops?`<b>${drops}</b> store${drops>1?'s':''} flagged now.`:'None flagged now.'}</div>`;
  host.innerHTML=note+TIERS.map(tr=>{
    const grp=rows.filter(r=>tierOf(r.v)===tr.key);
    if(!grp.length) return '';
    return `<div class="tierhead">${esc(tr.label)} <span class="tcount">${grp.length}</span></div>
      <table><thead><tr><th>Store</th><th>Visits/mo</th><th>MoM</th><th>Trend</th><th>US%</th><th>Since</th><th>Meta ads</th></tr></thead>
      <tbody>${grp.map(rowHtml).join('')}</tbody></table>`;
  }).join('');
}
function render(){renderBest();renderMovers();renderStores();}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  ['best','movers','stores'].forEach(id=>document.getElementById(id).style.display = id===t.dataset.tab?'':'none');
});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase().trim();render();};
document.getElementById('freshOnly').onchange=e=>{freshOnly=e.target.checked;render();};
stats();chips();render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default="snapshots")
    ap.add_argument("--movers", default="movers.json")
    ap.add_argument("--traffic", default="store_traffic.json")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args()

    stores, movers = collect(args.snapshots, args.movers, args.traffic)
    if not stores:
        print("no snapshots found", file=sys.stderr)
        return 1
    data = json.dumps({"stores": stores, "movers": movers}, ensure_ascii=False)
    # Escape sequences that could break out of the <script> block (a competitor
    # title containing "</script>" would otherwise inject arbitrary HTML/JS).
    # < etc. stay valid JSON and parse back to the original characters.
    data = (data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                .replace(" ", "\\u2028").replace(" ", "\\u2029"))
    html = HTML.replace("__DATA__", data)
    with open(args.out, "w") as f:
        f.write(html)
    enr = sum(1 for s in stores if s.get("traffic_meta"))
    print(f"wrote {args.out}  ({len(stores)} stores, {enr} with traffic"
          + (", movers included" if movers else ", no movers yet") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
