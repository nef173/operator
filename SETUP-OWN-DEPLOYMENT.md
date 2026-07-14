# Your own deployment (Nosura + Ovayla) — setup + multi-business updates

This is the SAME app code as the PrimeCore deployment. The ONLY thing that makes a
deployment "yours" vs "PrimeCore's" is **where it stores data**, never the code:

| Layer            | What it is                              | Per business?         |
|------------------|-----------------------------------------|-----------------------|
| **Code** (git)   | the FastAPI api + Next.js web           | SHARED — same for all |
| `DATABASE_URL`   | the Neon Postgres holding settings      | **separate per biz**  |
| `GOOGLE_STORES_DATA` | the data volume (queues, dossiers)  | **separate per biz**  |
| `TENANT`         | the display label                       | **separate per biz**  |
| **Connections**  | API keys + Shopify tokens (in the DB)   | **separate per biz**  |

Because credentials live in the **database**, not in git, a `git push` ships new CODE and
**never touches a single API key, Shopify token, or store**. That is the whole point of the
Connections design (see `api/app/connections.py` + `api/app/config.py`).

---

## A. First-time setup of YOUR deployment

### 1. Your own GitHub remote (PrimeCore's stays untouched)
```bash
cd operator-app
git remote rename origin primecore                 # keep the partner's, just rename it
git remote add origin https://github.com/<YOU>/operator-app.git
git push -u origin main
```

### 2. Your own Railway project
- `railway login` (your account), then create a project with TWO services:
  **api** (root `operator-app/api`, Dockerfile) + **web** (root `operator-app/web`).
- Add a **Neon Postgres** (or Railway Postgres) and set `DATABASE_URL` on the **api** service.
- Set on the **api** service: `TENANT=Nosura`, and a persistent `GOOGLE_STORES_DATA` volume.
- Set on the **web** service: `NEXT_PUBLIC_API_BASE=<your api URL>`.

### 3. Seed your credentials (ONE command, from this machine)
Reads the creds already on disk and writes them into YOUR new database. Leaves
Gemini / CJ empty on purpose (and `BRIGHTDATA_CUSTOMER_ID`, needed only for the server-side
Sponsored-PLA scan — see step 4).
```bash
cd operator-app/api
DATABASE_URL='postgresql://...your Neon...' .venv/bin/python scripts/seed_own_connections.py
```
Seeds: DataForSEO (user+pass), Bright Data token, TMAPI, **Nosura + Ovayla** Shopify domain
+ admin token. Re-runnable safely (merge-only — it never wipes a field you set later).

### 4. Paste what isn't on this machine
Open the live app → **Settings → Connections** and paste:
- **Gemini API Key** (vision · image gen · assistant — the one essential one)
- **`BRIGHTDATA_CUSTOMER_ID`** (only if you run the Google **Sponsored-PLA** scan) — the Sponsored-PLA
  capture is now **server-side via the Bright Data Scraping Browser** (`paid_shopping_scan_bd.py` →
  `GET /api/product-research/sponsored-plas`; needs the `playwright` dep in the API venv), driven by
  your already-seeded Bright Data token. Paste `BRIGHTDATA_CUSTOMER_ID` and click the BD provision
  button to auto-fill `BRIGHTDATA_BROWSER_CDP`. **No AdsPower** — it's been removed (the old local
  AdsPower profile is only a deprecated fallback).
- CJ — **leave empty** (that's PrimeCore's)

Done. Nosura + Ovayla are live with all your data providers.

---

## B. Pushing updates to BOTH businesses later (the answer to "without messing up connections")

You edit the code **once**. You push it to **both** remotes. Each business redeploys its own
code; **each keeps its own database, so connections / stores / keys are never touched.**

### One-time: make `git push` fan out to both remotes
```bash
cd operator-app
git remote set-url --add --push origin https://github.com/<YOU>/operator-app.git
git remote set-url --add --push origin https://github.com/PrimeCore24/operator-app.git
```
Now a single `git push origin main` pushes the same code to **both** GitHubs. If each Railway
project is connected to its own GitHub repo with auto-deploy on, both businesses redeploy the
new code automatically.

### Why connections survive every update
- A code push changes files in git. Credentials are **rows in each business's Neon DB**.
  A deploy that ships new code reads the SAME DB it always used → keys, tokens, stores intact.
- The seed script is **merge-only** (`connections.update` writes only supplied keys), so even
  re-running it never clears something you added in the UI.
- `TENANT` / `GOOGLE_STORES_DATA` / `DATABASE_URL` are **Railway env vars per project**, not in
  git — so pushing code can't cross-wire one business onto another's data.

### The one rule
Keep **secrets out of git** (they already are: `data/` is gitignored and the seed reads from
local `.env` files, never hardcodes). As long as that holds, "push updates to both" is just
`git push` — the businesses stay cleanly separated by their databases.
