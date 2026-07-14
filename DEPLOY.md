# Deploying the Operator app to Railway (live, from a GitHub push)

This is the build-ready setup. Everything in code is done; what's left is account-specific
(your GitHub, Railway, and Neon accounts), so the steps below are the hand-off.

---

## Architecture: one repo, two services, one database

```
GitHub repo  operator-app/
├── api/   ──►  Railway service "operator-api"   (FastAPI + in-process worker)
└── web/   ──►  Railway service "operator-web"   (Next.js standalone)
                       │
                       └──► Neon Postgres (free tier)  ← all persistent state
```

- **Two services from the same repo.** Each service has its own *Root Directory* and *Watch
  Path*, so a change under `api/` redeploys ONLY the API and a change under `web/` redeploys
  ONLY the web. That is the "separate the backend so I can work on it while it's live" part —
  you push to `api/`, the frontend keeps running untouched.
- **Neon Postgres holds all state** (jobs, runs, decisions, gameplans, settings, your saved
  Connections/credentials). Railway's container filesystem is **ephemeral** — every redeploy
  wipes it — so SQLite there would lose your data on each deploy. The app switches to Postgres
  automatically the moment `DATABASE_URL` is set (the adapter is wired in `api/app/db.py`).

---

## One-time setup

### 1. Database — Neon (free)
1. Create a project at neon.tech → copy the **pooled** connection string (the host contains
   `-pooler`), e.g. `postgresql://user:pass@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require`.
   The pooled string keeps you inside Neon's connection budget.
2. That's it — the app creates its own tables on first boot.

### 2. Push the code to GitHub
`operator-app/` is not yet a git repo. From inside it:
```bash
cd operator-app
git init
git add .
git commit -m "Operator app: API + web, Railway-ready"
git branch -M main
git remote add origin git@github.com:<you>/operator-app.git
git push -u origin main
```
The root `.gitignore` already excludes `.env`, `api/data/`, `node_modules/`, `.next/`, and
the local SQLite files, so no secrets or build junk get pushed.

### 3. Railway — create the project + two services
In a new Railway project, **New → GitHub Repo** twice (same repo), then configure each:

**Service A — operator-api**
- Settings → **Root Directory**: `api`
- Settings → **Watch Paths**: `/api/**` (so only API changes redeploy it)
- Build uses `api/Dockerfile` automatically (railway.json points at it).
- Variables:
  | Variable | Value |
  |---|---|
  | `DATABASE_URL` | your Neon **pooled** URL |
  | `CORS_ORIGINS` | the web service's public URL, e.g. `https://operator-web-production.up.railway.app` |
  | `GOOGLE_STORES_ROOT` | leave unset for now (see note below) |
- Generate a public domain (Settings → Networking → Generate Domain). Health check is at
  `/api/health`.

**Service B — operator-web**
- Settings → **Root Directory**: `web`
- Settings → **Watch Paths**: `/web/**`
- Build uses `web/Dockerfile` automatically.
- Variables:
  | Variable | Value |
  |---|---|
  | `NEXT_PUBLIC_API_BASE` | the **API** service's public URL, e.g. `https://operator-api-production.up.railway.app` |
- ⚠️ `NEXT_PUBLIC_API_BASE` is baked into the browser bundle **at build time**. If you change
  it, you must **redeploy** the web service (not just restart) for it to take effect.

### 4. Deploy order
1. Deploy **operator-api** first, grab its public URL.
2. Set `NEXT_PUBLIC_API_BASE` on the web service to that URL, set `CORS_ORIGINS` on the API
   to the web URL, then deploy **operator-web**.

> **Note on `GOOGLE_STORES_ROOT` (code) vs `GOOGLE_STORES_DATA` (data).** The API ships its
> CODE in the image (the pipeline scripts it runs as subprocesses). Its **live data** — store
> queues, dossiers, news snapshots, the SQLite run-log when you don't use Neon — is written at
> runtime and is what must PERSIST across redeploys. Two separate roots control this:
> - `GOOGLE_STORES_ROOT` — where the code/pipeline scripts live (defaults to the image; leave unset).
> - `GOOGLE_STORES_DATA` — where this deployment's live data is written. Point it at a **Railway
>   volume** so it survives redeploys (and so each business writes to its own volume). Unset = it
>   falls back to the code root (fine for a laptop run, not for Railway where the FS is ephemeral).
>
> With `DATABASE_URL` set (Neon), the DB state already persists; `GOOGLE_STORES_DATA` is what
> persists the remaining on-disk pipeline files. Set both for a durable live deploy.

---

## Running 3 (or N) businesses from ONE repo — "change the backend once, all go live"

This is the multi-business model. **One GitHub repo is the single source of truth for code;
each business is its own isolated deployment.** You fix a bug or add a feature once, `git push`,
and every business's deployment rebuilds from that same repo — no per-business code copies.

### What's shared vs isolated

| Shared across all businesses (change once) | Isolated per business (×N) |
|---|---|
| **Code** — the one GitHub repo (features, fixes) | Railway project/account |
| | **`DATABASE_URL`** → its own Neon database (credentials, runs, jobs, settings) |
| | **`GOOGLE_STORES_DATA`** → its own Railway volume (stores, listings, dossiers, news) |
| | **Connections** keys (Shopify, Google, Data, LLM) — entered in that deployment's Settings |
| | **`TENANT`** — a label so Settings shows which business this is |

Because each deployment runs in its **own container** with its **own database and its own data
volume**, business A can never see business B's stores, keys, listings, or run-log. The only thing
they have in common is the code.

### Which structure? → **ONE shared repo, N Railway projects** (recommended)

The whole goal is "change the code once → live for all businesses." That means the code must be
**one source of truth**, not copied per business. So:

- **ONE GitHub repo** holds the code.
- **Each business = its own Railway project** (its own account is fine), all pointed at that **same**
  repo's `main`. One `git push` → every Railway project rebuilds from the same commit.
- **Isolation lives in Railway env vars, NOT in the repo**: `DATABASE_URL` (own Neon DB),
  `GOOGLE_STORES_DATA` (own volume), `TENANT` (label), and the per-business keys you enter in the
  app's Settings. The repo is identical for everyone; only these per-project values differ.

**Don't give each business its own git repo.** Since the code is the same for all of them, separate
repos buy you nothing and cost you a lot: 3 places to keep in sync, push can partially fail, the
codebases silently drift, and you lose the "fix once" property that was the entire point. Only split
repos if a business needs genuinely *different code* or must live in a separate, non-shareable GitHub
org — neither applies here.

### Two ways to wire "push once → all deploy"

**Option A — each Railway project auto-deploys from the repo (simplest, recommended).**
Connect all 3 Railway projects (even across different Railway accounts) to the **same** GitHub
repo's `main` branch with auto-deploy on. One `git push` → all 3 rebuild. Each account just needs
read access to the repo (add as a collaborator, or use a deploy key).

**Option B — one GitHub Actions workflow deploys to all 3 (when you can't wire auto-deploy).**
Store each Railway account's token as a GitHub secret and fan out on every push to `main`:
```yaml
# .github/workflows/deploy.yml
on: { push: { branches: [main] } }
jobs:
  deploy:
    strategy:
      matrix:
        target: [biz1, biz2, biz3]   # each maps to a RAILWAY_TOKEN_<TARGET> secret
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm i -g @railway/cli
      - run: railway up --service operator-api --detach
        env: { RAILWAY_TOKEN: "${{ secrets[format('RAILWAY_TOKEN_{0}', matrix.target)] }}" }
```
Same result: one push → CI deploys all 3.

**Option C — separate GitHub repo per business, one local push fans out (NOT recommended).**
Only if you're forced into one-repo-per-business (e.g. separate GitHub orgs that can't share a
repo). Git lets a single remote carry multiple push URLs, so one `git push all` updates every repo:
```bash
git remote add all git@github.com:biz1/operator-app.git           # primary
git remote set-url --add --push all git@github.com:biz1/operator-app.git
git remote set-url --add --push all git@github.com:biz2/operator-app.git
git remote set-url --add --push all git@github.com:biz3/operator-app.git
git push all main     # → pushes the same commit to all three repos at once
```
Each repo's Railway project auto-deploys as usual. The catch this option carries (and why A is
better): a push can succeed on some repos and fail on others (one bad token / protected branch =
that business misses the deploy and silently drifts), and you now manage 3 sets of repo auth. The
isolation is identical to Option A — it still comes from each Railway project's env vars, not from
having separate repos — so you take on the downsides for no extra separation.

### Per-business setup (repeat for each of the 3)

In that business's Railway project, set on the **operator-api** service:

| Variable | Value (per business) |
|---|---|
| `DATABASE_URL` | that business's **own** Neon pooled URL |
| `GOOGLE_STORES_DATA` | the mount path of that business's Railway **volume**, e.g. `/data` |
| `TENANT` | a label, e.g. `acme` / `business-2` (shown in Settings → System → Deployment) |
| `CORS_ORIGINS` | that business's web URL |

Add a **Volume** to the api service (Settings → Volumes) and mount it at the same path you put in
`GOOGLE_STORES_DATA` (e.g. `/data`). Then open **Settings → System → Deployment** in the app to
confirm: it shows the tenant label and an **"isolated data volume"** badge when `GOOGLE_STORES_DATA`
is pointing at its own volume (vs the shared fallback).

Each business's API keys + Shopify tokens are entered once in **that deployment's** Settings →
Connections — they're stored in **that business's** database, never shared.

### Day-to-day after setup

- **Add a feature / fix a bug** → edit code once → `git push` → all 3 businesses get it.
- **Onboard business #4** → new Railway project + new Neon DB + new volume + its own `TENANT`,
  pointed at the same repo. No code change.
- **A business's data/keys stay private to it** — separate DB + separate volume = separate blast
  radius and separate billing.

---

## Keeping the bill predictable (storage + RAM)

What's already done in code to avoid overpaying:
- **No Railway volume.** All state lives in **Neon's free tier**, not a paid Railway volume.
- **History is pruned at startup** (`runlog.prune`) — newest ~10k runs/jobs and ~5k resolved
  decisions are kept, older terminal rows dropped. Live work is never pruned. This keeps Neon
  storage and query times flat over months.
- **Web ships as `output: "standalone"`** — the runtime image is the minimal server bundle,
  not all of `node_modules`, so the web container's RAM/disk footprint is small.
- **API runs a single uvicorn process, no `--reload`.** The always-on background worker lives
  *inside* that one process, so running multiple uvicorn workers would spawn duplicate
  schedulers — keep it at **one replica / one worker**. This is also the cheapest RAM profile.

Practical resource expectation: the API idles at a low baseline and the web standalone server
is light. Both comfortably fit a small instance; you scale the API's `OPERATOR_JOB_WORKERS`
(default 3) only if you run many concurrent auto-jobs.

---

## How Railway pricing / credits actually work

Railway bills **usage-based**, on top of a plan. You don't rent a fixed box — you pay for the
RAM-seconds and CPU-seconds your services actually consume, plus egress.

- **Trial:** a small one-time credit to try things; not meant for an always-on app.
- **Hobby — $5/month:** the $5 is a **subscription that includes $5 of usage credit**. Your
  services draw down that credit as they run. A light always-on app *can* fit near the $5
  if it's idle most of the time, but two always-on services (api + web) running 24/7 will
  typically consume **more than $5 of usage**, and you're billed the overage. Resource ceiling
  on Hobby is up to 8 GB RAM / 8 vCPU per service — plenty for this app.
- **Pro — $20/month:** $20 subscription that **includes $20 of usage credit**, higher limits,
  longer log/metric retention, more concurrency, and priority support. Same usage-based model
  on top.

**So is $20 the minimum?** Not strictly for *RAM/speed* — this app is light and Hobby's limits
(8 GB) already exceed what it needs, so Pro doesn't make it faster. The honest reason to pick a
plan is **how much 24/7 usage you'll burn**:
- If you keep **two always-on services**, the realistic monthly **usage** for two small
  containers running continuously tends to land in the **~$10–20** range. On **Hobby** you'd
  pay $5 + overage (so likely $10–20 total anyway); on **Pro** the $20 includes $20 of usage,
  so it's often the cleaner choice once you're genuinely always-on.
- Cheapest path while you're still iterating: stay on **Hobby**, and let the API/worker idle
  (it's cheap when idle). Move to **Pro** when you flip to true 24/7 operation or want the
  longer retention + headroom.

Two concrete levers to spend less regardless of plan:
1. **Neon free** for the database (already wired) — keeps DB cost at $0.
2. If you don't need the web UI up 24/7, you can let the **web** service sleep/scale to zero
   and keep only the **api** (which runs the worker) always-on — that roughly halves always-on
   usage. (Configure in the service's settings if/when Railway offers sleep for your plan.)

Bottom line: **Hobby ($5) is enough to go live and is cheapest while iterating; Pro ($20) is
the sensible step once both services run 24/7** — for budgeted usage credit + retention, not
for speed.
