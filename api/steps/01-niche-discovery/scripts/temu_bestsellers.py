#!/usr/bin/env python3
"""Temu best-sellers by category x time window via CDP Chrome (port 9222), US region.
SETUP (US):
  1) python us_proxy_forward.py &           # local no-auth proxy -> US upstream
  2) launch Chrome: --remote-debugging-port=9222 --remote-allow-origins=* \
       --user-data-dir=/tmp/temu_us_session --proxy-server=http://127.0.0.1:8888 \
       https://www.temu.com/channel/best-sellers.html
     (first run: solve ONE human verification puzzle; cookies then persist -> region=211 US)
USAGE: .venv/bin/python temu_bestsellers.py "Automotive" "Pet Supplies" ...
       TEMU_WINDOW="Within last 7 days" to change window.
NOTE: US shows prices as TEXT ($). TH region image-renders prices (use sold counts).
Category clicks MUST be real coordinate mouse events (CDP Input) - el.click() misses React.
"""
import json,os,sys,time,urllib.request,websocket
WIN=os.environ.get("TEMU_WINDOW","Within last 30 days")
t=[x for x in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json")) if x.get("type")=="page" and "temu" in x.get("url","")][0]
ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=50);mid=[0]
def cmd(m,p=None):
    mid[0]+=1;i=mid[0];ws.send(json.dumps({"id":i,"method":m,"params":p or {}}))
    while True:
        x=json.loads(ws.recv())
        if x.get("id")==i:return x
def ev(e):return cmd("Runtime.evaluate",{"expression":e,"returnByValue":True,"awaitPromise":True})["result"]["result"].get("value")
def click_xy(x,y):
    for ty in ("mousePressed","mouseReleased"):
        cmd("Input.dispatchMouseEvent",{"type":ty,"x":x,"y":y,"button":"left","clickCount":1});time.sleep(0.05)
def rect(text,top_min):
    r=ev(r"""(()=>{const o=[...document.querySelectorAll('*')].filter(e=>e.textContent.trim()===%s&&e.children.length===0&&e.offsetParent!==null&&e.getBoundingClientRect().top>%d);if(!o.length)return null;const r=o[0].getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()"""%(json.dumps(text),top_min))
    return json.loads(r) if r else None
EX=r"""(()=>{const o=[];const s=new Set();for(const a of document.querySelectorAll('a[href*="-g-"]')){const k=a.href.split('?')[0];if(s.has(k))continue;const c=a.closest('[class]');const t=c?(c.innerText||'').replace(/\s+/g,' ').replace(/Open in new tab\.?/,'').replace(/^Top pick/,'').replace(/^Local /,'').trim():'';if(!t||t.length<8)continue;s.add(k);let el=a,sold=null,price=null;for(let i=0;i<9&&el;i++){el=el.parentElement;if(!el)break;const x=el.innerText||'';if(!price){const pm=x.match(/\$\s?\d[\d,]*\.?\d{0,2}/);if(pm)price=pm[0].replace(/\s/g,'');}if(/sold/i.test(x)){const m=x.match(/([\d,.]+\s*[kK]?\+?)\s*sold/i);if(m){sold=m[1].replace(/\s+/g,'');break;}}}o.push({title:t.slice(0,48),price,sold,href:k});}return JSON.stringify(o.slice(0,20));})()"""
def sn(x):
    if not x:return -1
    x=x.lower().replace('+','').replace(',','')
    try:return float(x[:-1])*1000 if x.endswith('k') else float(x)
    except:return -1
print("region:",ev("(document.cookie.match(/region=\\d+/)||['?'])[0]"),"(211=US)")
cur="Recommended"
for cat in (sys.argv[1:] or ["Automotive"]):
    tr=rect(cur,40)
    if tr: click_xy(tr["x"],tr["y"]);time.sleep(2.5)
    op=rect(cat,140)
    if op: click_xy(op["x"],op["y"]);time.sleep(5);cur=cat
    for y in range(0,2000,700):ev(f"window.scrollTo(0,{y})");time.sleep(0.4)
    ev("window.scrollTo(0,0)");time.sleep(1)
    d=json.loads(ev(EX));d.sort(key=lambda r:sn(r['sold']),reverse=True)
    print(f"\n== US · {cat} · {WIN} ==")
    for r in d[:12]:print(f"  {(r['price'] or '-'):>8} {(r['sold'] or '-'):>9} sold  {r['title']}")
ws.close()
