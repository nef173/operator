#!/usr/bin/env python3
import json, re, time, urllib.request, websocket
DBG="http://127.0.0.1:9222"
t=[x for x in json.load(urllib.request.urlopen(DBG+"/json")) if x.get("type")=="page" and "temu" in x.get("url","")][0]
ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=40)
mid=[0]
def cmd(method,params=None):
    mid[0]+=1; i=mid[0]
    ws.send(json.dumps({"id":i,"method":method,"params":params or {}}))
    while True:
        m=json.loads(ws.recv())
        if m.get("id")==i: return m
def ev(expr):
    return cmd("Runtime.evaluate",{"expression":expr,"returnByValue":True,"awaitPromise":True})["result"]["result"].get("value")

for y in range(0,5000,700):
    ev(f"window.scrollTo(0,{y})"); time.sleep(0.6)
ev("window.scrollTo(0,0)"); time.sleep(1)

EXTRACT=r"""
(()=>{
  const out=[]; const seen=new Set();
  for(const a of document.querySelectorAll('a[href*="-g-"]')){
    const key=a.href.split('?')[0];
    if(seen.has(key)) continue;
    let el=a, card=null;
    for(let i=0;i<7&&el;i++){el=el.parentElement; if(el&&/sold/i.test(el.innerText||"")){card=el;break;}}
    const title=(a.getAttribute('aria-label')||a.title||'').replace(/Open in new tab\.?/,'').trim();
    if(!title) continue;
    seen.add(key);
    let sold=null, toppick=false;
    if(card){
      const m=(card.innerText||"").match(/([\d,.]+\s*[kK]?\+?)\s*sold/i);
      if(m) sold=m[1].replace(/\s+/g,'');
      toppick=/Top pick/i.test(card.innerText||"");
    }
    out.push({title:title.slice(0,75), sold, toppick, href:key});
  }
  return JSON.stringify(out);
})()
"""
data=json.loads(ev(EXTRACT))
def soldnum(s):
    if not s: return -1
    s=s.lower().replace('+','').replace(',','')
    try: return float(s[:-1])*1000 if s.endswith('k') else float(s)
    except: return -1
data.sort(key=lambda d: soldnum(d["sold"]), reverse=True)
print(f"=== Temu 'car trunk organizer' — {len(data)} products (sorted by sold) ===")
for d in data:
    tag="[TOP PICK]" if d["toppick"] else ""
    print(f"  {(d['sold'] or '-'):>9} sold  {tag:11} {d['title']}")
ws.close()
