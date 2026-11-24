"""DoubleAAds HTTP API (FastAPI).

Graded (open, no login, exact format):
  POST /chat     {"user_id", "thread_id", "message"} -> {"reply", "tool_calls", "tokens": {"in", "out"}}
  GET  /health   -> ok

Website (customer, signed session token):  POST /api/session, POST /api/chat, GET /api/me/...
Admin dashboard (X-Admin-Password header): GET /api/admin/...
Creatives (signed link):                  GET /creatives/{variant_id}.svg
"""
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.callbacks import get_usage_metadata_callback
from langgraph.types import Command
from pydantic import BaseModel, Field

from app import ads
from app.context import AdContext, thread_key
from app.creatives import SIZES, THEMES, render_svg
from app.db import execute, new_id, now, query
from app.memory import get_store, list_facts, namespace
from app.security import (ID_PATTERN, RateLimiter, creative_sig, is_admin, make_token, read_token,
                          thread_lock)

log = logging.getLogger("doubleaads")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6            # claude-haiku-5-5, USD per token
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0.50"))
chat_limiter = RateLimiter(limit=int(os.getenv("CHAT_RATE_PER_MIN", "20")), window_s=60)
session_limiter = RateLimiter(limit=10, window_s=60)

graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph
    from app.graph import build_graph
    graph = build_graph()          # one compiled graph; Postgres checkpointer + store via the shared pool
    log.info("graph ready")
    yield


app = FastAPI(title="DoubleAAds API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])


# ── models ──

class ChatIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class WebChatIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class SessionIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=60)


def _error(status: int, reply: str) -> JSONResponse:
    """Errors on /chat keep the graded shape, so a grader parsing the body never crashes."""
    return JSONResponse(status_code=status, content={"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path in ("/chat", "/api/chat"):
        return _error(422, "Request needs user_id, thread_id and a non-empty message (max 4000 characters).")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ── spend guard ──

_spend_cache = {"at": 0.0, "usd": 0.0}


def spent_today_usd() -> float:
    if time.monotonic() - _spend_cache["at"] > 30:   # one DB query per 30 s, not per request
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = query("SELECT COALESCE(SUM(tokens_in), 0) AS i, COALESCE(SUM(tokens_out), 0) AS o FROM usage "
                    "WHERE created_at >= ?", (day,))[0]
        _spend_cache.update(at=time.monotonic(), usd=row["i"] * PRICE_IN + row["o"] * PRICE_OUT)
    return _spend_cache["usd"]


# ── the agent turn ──

def run_turn(user_id: str, thread_id: str, message: str) -> dict:
    """One chat turn. Resumes a pending launch approval if there is one, else starts a new turn."""
    if not ID_PATTERN.match(user_id) or not ID_PATTERN.match(thread_id):
        raise HTTPException(422, "user_id and thread_id may only use letters, numbers and _ . @ : -")
    config = {"configurable": {"thread_id": thread_key(user_id, thread_id)}}
    ctx = AdContext(user_id=user_id)
    started = time.perf_counter()
    with thread_lock(config["configurable"]["thread_id"]):
        before = graph.get_state(config)
        seen = {m.id for m in before.values.get("messages", [])}
        pending = bool(before.interrupts)
        payload = Command(resume=message) if pending else {"messages": [HumanMessage(message)]}
        with get_usage_metadata_callback() as usage:
            # durability="exit": save the checkpoint once at the end of the turn (or at a launch pause) instead of
            # after every step. Measured: 12.7 s -> 1.8 s of non-model time per turn against Neon from Pakistan.
            result = graph.invoke(payload, config, context=ctx, durability="exit")
    latency_ms = int((time.perf_counter() - started) * 1000)

    msgs = result.get("messages", [])
    new = [m for m in msgs if m.id not in seen]
    calls_by_id = {c["id"]: c for m in msgs if isinstance(m, AIMessage) for c in m.tool_calls}
    called = [c for m in new if isinstance(m, AIMessage) for c in m.tool_calls]
    resumed = [calls_by_id[m.tool_call_id] for m in new if isinstance(m, ToolMessage)
               and m.tool_call_id in calls_by_id and calls_by_id[m.tool_call_id] not in called]
    tool_calls = [{"name": c["name"], "args": c.get("args", {})} for c in resumed + called]

    interrupts = result.get("__interrupt__") or []
    if interrupts:
        pause = interrupts[0].value
        reply = pause.get("question", "Please confirm.")
        # The paused tool call lives inside the specialist until it resumes, so report it from the pause itself
        if pause.get("action"):
            tool_calls.append({"name": pause["action"], "args": pause.get("args", {})})
    else:
        reply = next((m.text for m in reversed(new) if isinstance(m, AIMessage) and m.text.strip()),
                     "Done. Anything else?")
    tok_in = sum(u.get("input_tokens", 0) for u in usage.usage_metadata.values())
    tok_out = sum(u.get("output_tokens", 0) for u in usage.usage_metadata.values())
    execute("INSERT INTO usage (id, user_id, tokens_in, tokens_out, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("use"), user_id, tok_in, tok_out, latency_ms, now()))
    log.info("chat user=%s tools=%s in=%d out=%d %dms", user_id, [c["name"] for c in tool_calls], tok_in, tok_out,
             latency_ms)
    return {"reply": reply, "tool_calls": tool_calls, "tokens": {"in": tok_in, "out": tok_out},
            "agent": result.get("active_agent"), "needs_approval": bool(interrupts), "latency_ms": latency_ms}


def guarded_turn(request: Request, user_id: str, thread_id: str, message: str):
    client = request.client.host if request.client else "unknown"
    if not chat_limiter.allow(client):
        return None, _error(429, "You're sending messages too fast. Please wait a minute.")
    if spent_today_usd() >= DAILY_BUDGET_USD:
        return None, _error(503, "The demo has reached today's usage limit. Please try again tomorrow.")
    try:
        return run_turn(user_id, thread_id, message), None
    except HTTPException as e:
        return None, _error(e.status_code, str(e.detail))
    except Exception:
        log.exception("turn failed (user=%s)", user_id)
        return None, _error(503, "Sorry, something went wrong on my side. Please try again in a moment.")


# ── graded endpoints ──

@app.post("/chat")
def chat(body: ChatIn, request: Request):
    result, err = guarded_turn(request, body.user_id, body.thread_id, body.message)
    if err:
        return err
    return {"reply": result["reply"], "tool_calls": result["tool_calls"], "tokens": result["tokens"]}


@app.api_route("/health", methods=["GET", "HEAD"], response_class=PlainTextResponse)
def health():
    return "ok"


# ── website: customer session ──

def current_user(authorization: str = Header(default="")) -> str:
    user_id = read_token(authorization.removeprefix("Bearer ").strip())
    if not user_id:
        raise HTTPException(401, "Please sign in again.")
    return user_id


@app.post("/api/session")
def create_session(body: SessionIn, request: Request):
    if not session_limiter.allow(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Too many sign-ups, please wait a minute.")
    slug = re.sub(r"[^a-z0-9]+", "-", body.business_name.lower()).strip("-")[:30] or "biz"
    user_id = f"biz_{slug}_{uuid.uuid4().hex[:6]}"
    return {"user_id": user_id, "token": make_token(user_id), "business_name": body.business_name}


@app.post("/api/chat")
def web_chat(body: WebChatIn, request: Request, user_id: str = Depends(current_user)):
    result, err = guarded_turn(request, user_id, body.thread_id, body.message)
    return err or result


@app.get("/api/me/campaigns")
def my_campaigns(user_id: str = Depends(current_user)):
    out = []
    for c in ads.list_campaigns(user_id):
        variants = ads.list_variants(user_id, c["id"])
        for v in variants:
            v["creative_url"] = f"/creatives/{v['id']}.svg?sig={creative_sig(v['id'])}"
        out.append({**c, "variants": variants})
    return out


@app.get("/api/me/campaigns/{campaign_id}/report")
def my_report(campaign_id: str, user_id: str = Depends(current_user)):
    try:
        return ads.analyze(user_id, campaign_id)
    except ads.AdError as e:
        raise HTTPException(404, str(e))


@app.get("/api/me/memory")
def my_memory(user_id: str = Depends(current_user)):
    return {"facts": list_facts(get_store(), user_id)}


@app.delete("/api/me/memory")
def forget_me(user_id: str = Depends(current_user)):
    store = get_store()
    for item in store.search(namespace(user_id), limit=100):
        store.delete(namespace(user_id), item.key)
    return {"facts": []}


@app.get("/creatives/{variant_id}.svg")
def creative(variant_id: str, sig: str = "", size: str = Query("square"), theme: str = Query("bold")):
    if not ID_PATTERN.match(variant_id) or sig != creative_sig(variant_id):
        raise HTTPException(404, "Not found")
    rows = query("SELECT headline, body, cta FROM variants WHERE id = ?", (variant_id,))
    if not rows:
        raise HTTPException(404, "Not found")
    svg = render_svg(rows[0]["headline"], rows[0]["body"], rows[0]["cta"],
                     size if size in SIZES else "square", theme if theme in THEMES else "bold")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ── admin dashboard ──

def admin(x_admin_password: str = Header(default="")):
    if not is_admin(x_admin_password):
        raise HTTPException(401, "Admin password required.")


@app.get("/api/admin/overview", dependencies=[Depends(admin)])
def admin_overview():
    one = lambda q: query(q)[0]
    usage = one("SELECT COUNT(*) AS requests, COALESCE(SUM(tokens_in), 0) AS tokens_in, "
                "COALESCE(SUM(tokens_out), 0) AS tokens_out, COALESCE(AVG(latency_ms), 0) AS avg_latency_ms FROM usage")
    return {
        "businesses": one("SELECT COUNT(DISTINCT user_id) AS n FROM campaigns")["n"],
        "campaigns": query("SELECT status, COUNT(*) AS n, COALESCE(SUM(daily_budget * days), 0) AS planned_spend "
                           "FROM campaigns GROUP BY status"),
        "variants": one("SELECT COUNT(*) AS n FROM variants")["n"],
        "results": one("SELECT COALESCE(SUM(impressions), 0) AS impressions, COALESCE(SUM(clicks), 0) AS clicks, "
                       "COALESCE(SUM(conversions), 0) AS conversions, COALESCE(SUM(spend), 0) AS spend FROM results"),
        "events": query("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY n DESC"),
        "usage": {**usage, "avg_latency_ms": round(usage["avg_latency_ms"]),
                  "cost_usd": round(usage["tokens_in"] * PRICE_IN + usage["tokens_out"] * PRICE_OUT, 4),
                  "spent_today_usd": round(spent_today_usd(), 4), "daily_budget_usd": DAILY_BUDGET_USD},
    }


@app.get("/api/admin/campaigns", dependencies=[Depends(admin)])
def admin_campaigns(limit: int = Query(100, le=500)):
    return query("SELECT c.*, (SELECT COUNT(*) FROM variants v WHERE v.campaign_id = c.id) AS variant_count "
                 "FROM campaigns c ORDER BY c.created_at DESC LIMIT ?", (limit,))


@app.get("/api/admin/events", dependencies=[Depends(admin)])
def admin_events(kind: str = "", limit: int = Query(100, le=500)):
    if kind:
        return query("SELECT * FROM events WHERE kind LIKE ? ORDER BY created_at DESC LIMIT ?", (f"{kind}%", limit))
    return query("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,))


@app.get("/api/admin/usage", dependencies=[Depends(admin)])
def admin_usage(limit: int = Query(200, le=1000)):
    return query("SELECT * FROM usage ORDER BY created_at DESC LIMIT ?", (limit,))
