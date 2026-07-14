"""Assistant — a read-only copilot for the Google Stores operator pipeline.

The assistant is a CHEAP-MODEL chat layer that can read all pipeline state and
PROPOSE actions, but it NEVER executes a state-changing action itself. Every
proposed action it returns is rendered in the UI as a confirm-button that routes
through the SAME existing, logged, confirmed control endpoints a manual button
uses (POST /api/jobs, PUT /api/autonomy/<step>, decision approve/reject, worker
tick/enable). The operator stays the decision node.

Design contract (the whole point):
  - chat() gathers a compact JSON snapshot of CURRENT state by calling the same
    reader/worker/costs functions the read endpoints use.
  - It asks the cheap agent model (costs.agent_model()) for a short answer plus,
    when appropriate, a structured list of `proposed_actions`.
  - It VALIDATES every proposed action server-side against an allowlist of action
    types that each map 1:1 to an existing confirmed endpoint, dropping anything
    that isn't recognised or whose job spec isn't a real JOB_SPECS id.
  - It returns the actions to the UI. It does NOT run any of them.

LLM call path: there is no LLM library bundled in the api venv (the heavy pipeline
steps run as manual jobs in the operator's own Claude Code). So this module talks
to an OpenAI-compatible chat-completions endpoint (a LiteLLM proxy / gateway) over
stdlib urllib, configured by env vars:
    ASSISTANT_LLM_BASE_URL  (e.g. http://localhost:4000/v1  — a LiteLLM proxy)
    ASSISTANT_LLM_API_KEY   (the proxy / provider key)
    ASSISTANT_LLM_MODEL     (optional override; defaults to costs.agent_model())
If those aren't configured (or the call fails), chat() degrades gracefully: it
returns a deterministic, state-grounded summary reply with NO proposed actions,
rather than crashing. This keeps `import app.main` clean with zero new deps and
lets the operator wire the real model later by setting the env vars.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import connections, costs, jobs, readers, runlog, worker

# ------------------------------------------------------------------ allowlist
# Every proposable action type maps 1:1 to an EXISTING confirmed control endpoint.
# The backend never executes these — it only returns them so the UI can render a
# confirm-button that calls the matching api client method.
ALLOWED_ACTION_TYPES = {
    "run_job",          # → POST /api/jobs                (api.createJob)
    "set_autonomy",     # → PUT  /api/autonomy/<step>     (api.setAutonomy)
    "approve_decision", # → POST /api/decisions/<id>/approve (api.approveDecision)
    "reject_decision",  # → POST /api/decisions/<id>/reject  (api.rejectDecision)
    "worker_tick",      # → POST /api/worker/tick         (api.workerTick)
    "set_worker_enabled",  # → POST /api/worker/enable    (api.setWorkerEnabled)
    "navigate",         # pure client navigation, no mutation
}

_MAX_TOKENS = 1500  # headroom so a reasoning/"thinking" model still emits content after it thinks
_SNAPSHOT_DECISION_TITLES = 12
_SNAPSHOT_JOB_ROWS = 8
_SNAPSHOT_LEARNINGS = 12

# Feedback the operator gives on a job's OUTPUT after the assistant ran it (approve / reject
# + a free-text note like "this is bad, look more into X"). Stored as a durable LEARNING under
# this kind so every future assistant turn reads it back — this is the "context + learning" loop.
_FEEDBACK_KIND = "assistant"
_FEEDBACK_VERDICTS = {"approve", "reject", "refine"}


# ------------------------------------------------------------------ project data/knowledge
def _queue_brief(q: dict | None, item_key_candidates: tuple[str, ...]) -> dict:
    """Compact summary of a stdlib JSON queue (candidate-/listing-queue): total count
    + a per-state tally + a few example names. Defensive — shape varies per queue."""
    if not isinstance(q, dict):
        return {"count": 0}
    items = None
    for k in item_key_candidates:
        v = q.get(k)
        if isinstance(v, list):
            items = v
            break
    if items is None:
        # Some queues nest items as a dict keyed by id/slug.
        for k in item_key_candidates:
            v = q.get(k)
            if isinstance(v, dict):
                items = list(v.values())
                break
    if items is None:
        return {"count": 0}
    by_state: dict[str, int] = {}
    names: list[str] = []
    for it in items:
        if isinstance(it, dict):
            st = it.get("state") or it.get("status")
            if st:
                by_state[st] = by_state.get(st, 0) + 1
            nm = it.get("keyword") or it.get("slug") or it.get("title") or it.get("name")
            if nm and len(names) < 8:
                names.append(str(nm))
    return {"count": len(items), "by_state": by_state, "examples": names}


def _project_brief(store: str | None) -> dict:
    """A compact picture of the project's OWN data/knowledge on disk — research
    dossiers, the discovery candidate queue, the listing queue, the competitor spy
    roster, niche launches. This is what 'answer from the project first' means: the
    assistant grounds in these real artifacts before anything else (it has no web)."""
    brief: dict = {}

    try:
        dossiers = readers.list_dossiers()
        brief["dossiers"] = {
            "count": len(dossiers),
            "slugs": [d.get("slug") for d in dossiers if d.get("slug")][:20],
        }
    except Exception:
        brief["dossiers"] = {"count": 0}

    try:
        roster = readers.spy_roster()
        rstores = roster.get("stores") if isinstance(roster, dict) else None
        brief["competitor_spy_roster"] = {"count": len(rstores) if isinstance(rstores, list) else 0}
    except Exception:
        brief["competitor_spy_roster"] = {"count": 0}

    try:
        launches = readers.list_niche_launches()
        brief["niche_launches"] = {
            "count": len(launches),
            "slugs": [l.get("slug") for l in launches if isinstance(l, dict) and l.get("slug")][:20],
        }
    except Exception:
        brief["niche_launches"] = {"count": 0}

    # Per active-store queues (the research -> candidate -> listing spine).
    if store:
        try:
            brief["candidate_queue"] = _queue_brief(
                readers.candidate_queue(store), ("candidates", "items", "queue"))
        except Exception:
            brief["candidate_queue"] = {"count": 0}
        try:
            brief["listing_queue"] = _queue_brief(
                readers.listing_queue(store), ("categories", "listings", "items", "queue"))
        except Exception:
            brief["listing_queue"] = {"count": 0}

    return brief


# ------------------------------------------------------------------ feedback encode/decode
# A feedback learning's `reason` column stores "[verdict] note" so one row carries both the
# approve/reject signal and the operator's free-text. These two helpers round-trip that shape.
def _encode_reason(verdict: str, note: str | None) -> str:
    note = (note or "").strip()
    return f"[{verdict}] {note}".strip()


def _verdict_of(reason: str | None) -> str | None:
    if not reason or not reason.startswith("["):
        return None
    end = reason.find("]")
    return reason[1:end] if end != -1 else None


def _note_of(reason: str | None) -> str:
    if not reason:
        return ""
    if reason.startswith("[") and "]" in reason:
        return reason[reason.find("]") + 1 :].strip()
    return reason.strip()


# ------------------------------------------------------------------ state snapshot
def _snapshot(store: str | None) -> dict:
    """A compact JSON picture of CURRENT pipeline state, gathered from the same
    reader/worker/costs functions the read endpoints use. Kept small on purpose so
    the cheap model has the facts it needs without a huge prompt."""
    try:
        stores = readers.list_stores()
    except Exception:
        stores = []

    # Worker / heartbeat
    try:
        wstatus = worker.status()
    except Exception:
        wstatus = {}
    wstate = wstatus.get("worker") or {}
    counts = wstatus.get("counts") or {}

    # Pending decisions (titles only — that's what the operator asks about)
    try:
        pending = runlog.decisions_list(status="pending", store=store, limit=50)
    except Exception:
        pending = []
    pending_brief = [
        {"id": d.get("id"), "title": d.get("title"),
         "store": d.get("store"), "kind": d.get("kind")}
        for d in pending[:_SNAPSHOT_DECISION_TITLES]
    ]

    # Running / queued / needs-operator jobs
    def _job_brief(rows: list[dict]) -> list[dict]:
        return [
            {"id": j.get("id"), "spec": j.get("spec"), "title": j.get("title"),
             "status": j.get("status"), "store": j.get("store")}
            for j in (rows or [])[:_SNAPSHOT_JOB_ROWS]
        ]

    # Autonomy step modes
    try:
        steps = worker.config()
    except Exception:
        steps = []
    step_modes = [
        {"step": s.get("step"), "title": s.get("title"), "surface": s.get("surface"),
         "mode": s.get("mode"), "cadence": s.get("cadence")}
        for s in steps
    ]

    # Job specs the assistant may propose to run
    try:
        spec_rows = jobs.specs()
    except Exception:
        spec_rows = []
    spec_brief = [
        {"id": s.get("id"), "title": s.get("title"),
         "mode": s.get("mode"), "surface": s.get("surface"), "summary": s.get("summary")}
        for s in spec_rows
    ]

    # Cost summary (cheap, scale-aware overview)
    try:
        cov = costs.overview(store=store)
        cost_brief = {
            "agent_model": cov.get("agent", {}).get("selected"),
            "per_listing_est_usd": cov.get("per_listing", {}).get("est_cost"),
            "fixed_monthly_usd": cov.get("fixed_monthly", {}).get("total"),
            "spend_to_date_usd": cov.get("spend_to_date", {}).get("total"),
            "projected_monthly_usd": cov.get("projection", {}).get("projected_monthly"),
        }
    except Exception:
        cost_brief = {}

    # Operator feedback on past assistant-run outputs (approve/reject + the note). This is the
    # learning the assistant must honour: don't re-propose what was rejected; keep what worked;
    # act on "look more into X" notes. Newest first; store-scoped + global.
    try:
        learns = runlog.learnings_recent(limit=_SNAPSHOT_LEARNINGS, kind=_FEEDBACK_KIND, store=store)
    except Exception:
        learns = []
    operator_learnings = [
        {"verdict": _verdict_of(l.get("reason")), "about": l.get("signal"),
         "note": _note_of(l.get("reason")), "store": l.get("store"), "ts": l.get("ts")}
        for l in learns
    ]

    return {
        "stores": stores,
        "active_store": store,
        "operator_learnings": operator_learnings,
        "worker": {
            "enabled": wstate.get("enabled"),
            "status": wstate.get("status"),
            "last_tick": wstate.get("last_tick"),
            "ticks": wstate.get("ticks"),
        },
        "counts": counts,
        "pending_decisions": pending_brief,
        "running_jobs": _job_brief(wstatus.get("running")),
        "queued_jobs": _job_brief(wstatus.get("queued")),
        "needs_operator_jobs": _job_brief(wstatus.get("needs_operator_jobs")),
        "scheduled_steps": [
            {"step": s.get("step"), "title": s.get("title"),
             "mode": s.get("mode"), "cadence": s.get("cadence")}
            for s in (wstatus.get("scheduled") or [])
        ],
        "autonomy_steps": step_modes,
        "job_specs": spec_brief,
        "costs": cost_brief,
        "project_data": _project_brief(store),
    }


# ------------------------------------------------------------------ prompt
def _system_prompt(snapshot: dict) -> str:
    job_spec_ids = sorted({s["id"] for s in snapshot.get("job_specs", []) if s.get("id")})
    step_ids = sorted({s["step"] for s in snapshot.get("autonomy_steps", []) if s.get("step")})
    return (
        "You are the Assistant for the Google Stores operator app — a READ-ONLY copilot for a "
        "Google-Shopping dropshipping pipeline (research -> sourcing -> listing -> ads -> scale).\n\n"
        "HARD RULE: you NEVER execute or perform a state-changing action yourself. You can only "
        "ANSWER questions and PROPOSE actions. Every action you propose is rendered in the UI as a "
        "confirm-button the operator must click; the click routes through the app's existing logged, "
        "confirmed control endpoints. The operator is always the decision node. Do not claim you ran "
        "anything; say you are proposing it.\n\n"
        "GROUNDING RULE (most important): ALWAYS answer from THIS PROJECT'S OWN DATA AND KNOWLEDGE "
        "FIRST — the CURRENT STATE snapshot below (worker, jobs, decisions, costs, autonomy steps) AND "
        "the `project_data` section (research dossiers, the candidate/listing queues, the competitor "
        "spy roster, niche launches). You have NO web access and you do NOT search the internet or run "
        "anything on your own — never imply that you did. Check the project's data first; refer to its "
        "real dossiers, queue entries, decisions, jobs, and costs by their actual names/ids. If the "
        "answer genuinely isn't in the project's data, say plainly what's missing and PROPOSE a job "
        "(e.g. a research/scan job) that would gather it through the pipeline — do NOT fabricate facts, "
        "guess, or substitute outside/general knowledge for the project's real state.\n\n"
        "LEARNING RULE: the snapshot's `operator_learnings` list is the operator's verdicts on "
        "outputs you produced before — each is a verdict (approve/reject/refine), what it was "
        "about (a job spec), and a free-text note (e.g. \"this is bad, look more into long-tail "
        "terms\"). HONOUR them: don't re-propose work the operator rejected for the same reason, "
        "repeat what they approved, and when a note says to look deeper or change approach, fold "
        "that into your answer and any new proposed action's args. Cite the relevant note briefly "
        "when it shapes your reply so the operator sees you remembered.\n\n"
        "Be concise and practical.\n\n"
        "CURRENT STATE (JSON snapshot):\n"
        f"{json.dumps(snapshot, default=str)}\n\n"
        "When (and only when) an action would help, include a `proposed_actions` list. Each item MUST "
        "be one of these exact shapes (these are the ONLY allowed types — anything else is dropped):\n"
        '  {"type":"run_job","spec":"<job_spec_id>","store":"<store>","args":{...optional},"label":"..."}\n'
        '  {"type":"set_autonomy","step":"<step>","mode":"manual|suggest|auto","cadence":"on-demand|daily|weekly","label":"..."}\n'
        '  {"type":"approve_decision","id":<decision_id>,"label":"..."}\n'
        '  {"type":"reject_decision","id":<decision_id>,"label":"..."}\n'
        '  {"type":"worker_tick","store":"<store>","label":"..."}\n'
        '  {"type":"set_worker_enabled","enabled":true|false,"label":"..."}\n'
        '  {"type":"navigate","href":"/some-route","label":"..."}\n\n'
        f"Valid job_spec ids: {job_spec_ids}\n"
        f"Valid autonomy step ids: {step_ids}\n"
        f"Valid stores: {snapshot.get('stores')}\n"
        "Every proposed action needs a short human `label` for its confirm-button (e.g. \"Run trend radar for nosura\").\n"
        "Default the store to the active_store when a store is required and the user didn't name one.\n\n"
        "OUTPUT FORMAT: reply with a short natural-language answer for the operator. If you want to "
        "propose actions, append — on its own line at the very end — a single fenced JSON block:\n"
        "```json\n{\"proposed_actions\": [ ... ]}\n```\n"
        "If there are no actions to propose, omit the JSON block entirely. Never invent decision ids, "
        "job specs, or steps that aren't in the snapshot."
    )


# ------------------------------------------------------------------ LLM call (stdlib)
def _llm_configured() -> bool:
    return bool(connections.runtime_get("ASSISTANT_LLM_BASE_URL") and connections.runtime_get("ASSISTANT_LLM_API_KEY"))


def _call_llm(system: str, messages: list[dict]) -> str:
    """POST to an OpenAI-compatible /chat/completions endpoint (a LiteLLM proxy /
    gateway) over stdlib urllib. Returns the assistant message text. Raises on any
    transport/parse problem so the caller can fall back."""
    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").rstrip("/")
    key = connections.runtime_get("ASSISTANT_LLM_API_KEY") or ""
    # The chat copilot follows the operator's model SELECTOR (Settings → Costs) spelled for
    # the active gateway; translate/multimarket/vision deliberately do NOT (locked Gemini tier).
    model = connections.agent_text_model() or costs.agent_model()
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": _MAX_TOKENS,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content")
    # Some gateways return content as a list of parts ([{type:text, text:...}]); flatten it.
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


# ------------------------------------------------------------------ parse + validate
def _extract_json_block(text: str) -> dict | None:
    """Pull the proposed_actions JSON out of the model's reply. Tolerates a fenced
    ```json block, a bare {...} block, or its complete absence. Defensive: any parse
    failure returns None (→ no proposed actions), never raises."""
    if not text:
        return None
    # Prefer a fenced ```json ... ``` block.
    fence = "```"
    if fence in text:
        for chunk in text.split(fence):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{") and '"proposed_actions"' in c:
                try:
                    return json.loads(c)
                except (json.JSONDecodeError, ValueError):
                    continue
    # Fall back to the last balanced {...} that mentions proposed_actions.
    idx = text.rfind('"proposed_actions"')
    if idx == -1:
        return None
    start = text.rfind("{", 0, idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _strip_json_block(text: str) -> str:
    """Remove the trailing fenced JSON block from the natural-language reply so the
    operator sees clean prose; the actions render as buttons separately."""
    fence = "```"
    if fence not in text:
        return text.strip()
    head = text.split(fence)[0]
    return head.strip() or text.strip()


def _validate_actions(raw: object, snapshot: dict) -> list[dict]:
    """Drop any proposed action whose type isn't in the allowlist, or whose target
    (job spec / step / store / decision id) isn't real in the snapshot. The backend
    NEVER runs these; this just keeps the UI from rendering a dead/unsafe button."""
    if not isinstance(raw, list):
        return []
    valid_specs = {s["id"] for s in snapshot.get("job_specs", []) if s.get("id")}
    valid_steps = {s["step"] for s in snapshot.get("autonomy_steps", []) if s.get("step")}
    valid_stores = set(snapshot.get("stores") or [])
    valid_modes = {"manual", "suggest", "auto"}
    valid_cadences = {"on-demand", "daily", "weekly"}

    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        atype = item.get("type")
        if atype not in ALLOWED_ACTION_TYPES:
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue

        if atype == "run_job":
            spec = item.get("spec")
            if spec not in valid_specs:
                continue
            store = item.get("store")
            if store is not None and store not in valid_stores:
                continue
            action = {"type": atype, "label": label.strip(), "spec": spec, "store": store}
            if isinstance(item.get("args"), dict):
                action["args"] = item["args"]
            out.append(action)

        elif atype == "set_autonomy":
            step = item.get("step")
            if step not in valid_steps:
                continue
            mode = item.get("mode")
            cadence = item.get("cadence")
            if mode is not None and mode not in valid_modes:
                continue
            if cadence is not None and cadence not in valid_cadences:
                continue
            if mode is None and cadence is None:
                continue
            out.append({"type": atype, "label": label.strip(), "step": step,
                        "mode": mode, "cadence": cadence})

        elif atype in ("approve_decision", "reject_decision"):
            try:
                did = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            out.append({"type": atype, "label": label.strip(), "id": did})

        elif atype == "worker_tick":
            store = item.get("store")
            if store is not None and store not in valid_stores:
                continue
            out.append({"type": atype, "label": label.strip(), "store": store})

        elif atype == "set_worker_enabled":
            enabled = item.get("enabled")
            if not isinstance(enabled, bool):
                continue
            out.append({"type": atype, "label": label.strip(), "enabled": enabled})

        elif atype == "navigate":
            href = item.get("href")
            if not isinstance(href, str) or not href.startswith("/"):
                continue
            out.append({"type": atype, "label": label.strip(), "href": href})

    return out


# ------------------------------------------------------------------ fallback
def _fallback_reply(snapshot: dict) -> str:
    """Deterministic, state-grounded reply when no LLM endpoint is configured / it
    fails. Summarises what needs attention so the page is still useful offline."""
    c = snapshot.get("counts") or {}
    pending = snapshot.get("pending_decisions") or []
    lines = ["The assistant model isn't configured, so here's a direct read of current state:"]
    lines.append(
        f"- {c.get('pending_decisions', len(pending))} decision(s) waiting on you, "
        f"{c.get('running', 0)} job(s) running, {c.get('queued', 0)} queued, "
        f"{c.get('needs_operator', 0)} manual handoff(s)."
    )
    if pending:
        titles = ", ".join(d.get("title", "?") for d in pending[:5])
        lines.append(f"- Pending: {titles}.")
    pd = snapshot.get("project_data") or {}
    doss = pd.get("dossiers") or {}
    cq = pd.get("candidate_queue") or {}
    lq = pd.get("listing_queue") or {}
    if doss or cq or lq:
        lines.append(
            f"- Project data: {doss.get('count', 0)} research dossier(s), "
            f"{cq.get('count', 0)} discovery candidate(s), "
            f"{lq.get('count', 0)} listing-queue entr(ies) for the active store."
        )
    cost = snapshot.get("costs") or {}
    if cost.get("per_listing_est_usd") is not None:
        lines.append(
            f"- Cost: ~${cost.get('per_listing_est_usd')}/listing (build), "
            f"~${cost.get('fixed_monthly_usd')}/mo fixed infra, model {cost.get('agent_model')}."
        )
    lines.append(
        "Set ASSISTANT_LLM_BASE_URL + ASSISTANT_LLM_API_KEY (a LiteLLM proxy) to enable "
        "the conversational copilot and action proposals."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ public entrypoint
def chat(messages: list[dict], store: str | None = None) -> dict:
    """Run one assistant turn.

    `messages` is the full conversation as [{role, content}, ...]. We build a system
    prompt embedding a fresh state snapshot, call the cheap agent model, then parse +
    server-side-validate any proposed_actions. We return:
        {"reply": str, "proposed_actions": [ {type, label, ...}, ... ]}
    The backend NEVER runs an action — the UI renders each as a confirm-button that
    calls an existing control endpoint. Validation drops anything off-allowlist.
    """
    # Sanitize the conversation to the two roles the chat API accepts.
    convo: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            convo.append({"role": role, "content": content})
    if not convo:
        convo = [{"role": "user", "content": "What needs my attention right now?"}]

    snapshot = _snapshot(store)

    if not _llm_configured():
        return {"reply": _fallback_reply(snapshot), "proposed_actions": []}

    system = _system_prompt(snapshot)
    try:
        raw_text = _call_llm(system, convo)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError, OSError):
        return {
            "reply": _fallback_reply(snapshot)
            + "\n\n(The configured assistant model could not be reached on this turn.)",
            "proposed_actions": [],
        }

    block = _extract_json_block(raw_text)
    actions = _validate_actions((block or {}).get("proposed_actions"), snapshot)
    # Never surface an empty "(no reply)": if the model returned only a JSON block (or nothing
    # usable), fall back to the deterministic state summary so the operator always gets an answer.
    reply = _strip_json_block(raw_text)
    if not reply:
        reply = _fallback_reply(snapshot)
    return {"reply": reply, "proposed_actions": actions}


# ------------------------------------------------------------------ feedback (learning loop)
def record_feedback(
    verdict: str,
    note: str | None = None,
    spec: str | None = None,
    store: str | None = None,
    job_id: int | None = None,
) -> dict:
    """Record the operator's verdict on an output the assistant produced (e.g. a keyword-search
    job result): approve / reject / refine, plus a free-text note. Persisted as a durable
    LEARNING (kind="assistant") so every future assistant turn reads it back in `_snapshot` and
    adapts. This is the "context + learning" the operator asked for. Returns the stored row.

    `verdict` is required and must be one of approve/reject/refine; everything else is optional.
    Raises ValueError on a bad verdict so the route can return 400.
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in _FEEDBACK_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_FEEDBACK_VERDICTS)}")
    learning_id = runlog.learning_add(
        kind=_FEEDBACK_KIND,
        reason=_encode_reason(verdict, note),
        store=store,
        signal=spec or None,
        action=(f"job:{job_id}" if job_id is not None else None),
    )
    return {
        "ok": True,
        "learning_id": learning_id,
        "verdict": verdict,
        "about": spec,
        "note": (note or "").strip(),
        "store": store,
    }
