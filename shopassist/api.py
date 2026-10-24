"""HTTP API + web UI (FastAPI). One process serves the JSON API and the static front-end in web/.

Auth is simplified for the demo: X-Customer-Id stands in for the subject of a verified login token
(JWT), and the support-desk routes need X-Admin-Key unless DEMO_MODE is on. The property that matters
holds either way: the customer identity comes from authentication, never from the chat text or the LLM.

A public deployment on a free LLM key also needs abuse protection, so there is an optional access code,
a per-IP rate limit, a per-conversation turn cap and a daily turn cap.

Run:  uvicorn shopassist.api:app --reload   ->  http://127.0.0.1:8000  (API docs at /docs)
"""
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm
from .config import ROOT, money, settings
from .db.seed import seed
from .policies import policy_label
from .service import SupportService, result_to_dict

WEB_DIR = ROOT / "web"
REPORTS_DIR = ROOT / "evals" / "reports"
DISCLAIMER = ("Unofficial portfolio demo, not affiliated with Amazon. Store policies come from Amazon.in's public "
              "help pages; customers and orders are synthetic.")
service: SupportService | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global service
    if not settings.db_path.exists():
        seed(settings.db_path)
    service = SupportService()  # loads DB, knowledge-base index and models once per process
    if settings.demo_mode:
        service.refresh_if_stale()
    yield


app = FastAPI(title="ShopAssist API", version="1.1.0", lifespan=lifespan)
if settings.cors_origins:  # only when the UI is served from another origin; same-origin needs no CORS
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-Demo-Code", "X-Customer-Id", "X-Admin-Key"])


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class Decision(BaseModel):
    approve: bool
    agent_name: str = "specialist"


# ---------------- dependencies: access, identity, abuse limits ----------------
_ip_hits: dict[str, deque] = defaultdict(deque)
_ip_lock = threading.Lock()


def access(x_demo_code: str | None = Header(None)):
    if settings.demo_access_code and x_demo_code != settings.demo_access_code:
        raise HTTPException(401, "access code required")


def customer(x_customer_id: str | None = Header(None), _: None = Depends(access)) -> str:
    if not x_customer_id or not service.repo.get_customer(x_customer_id):
        raise HTTPException(401, "not authenticated")
    return x_customer_id


def admin(x_admin_key: str | None = Header(None), _: None = Depends(access)):
    if not settings.demo_mode and x_admin_key != settings.admin_api_key:
        raise HTTPException(403, "admin key required")


def rate_limited(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _ip_lock:
        hits = _ip_hits[ip]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= settings.demo_requests_per_ip_per_min:
            raise HTTPException(429, "Too many messages. Please wait a minute.")
        hits.append(now)


def owned_session(conversation_id: str, customer_id: str):
    try:
        session = service.get_session(conversation_id)
    except KeyError:
        raise HTTPException(404, "conversation not found")
    if session.customer["id"] != customer_id:  # same 404 as "missing": don't reveal it exists
        raise HTTPException(404, "conversation not found")
    return session


# ---------------- public ----------------
@app.get("/health")
def health():
    return {"status": "ok", "model": settings.llm_model, "kb_chunks": len(service.kb.chunks), "llm_quota": llm.quota_status()}


@app.get("/v1/demo/config")
def demo_config():
    return {"store": settings.store_name, "demo_mode": settings.demo_mode, "model": settings.llm_model,
            "access_code_required": bool(settings.demo_access_code), "disclaimer": DISCLAIMER,
            "kb_pages": len({c["doc_id"] for c in service.kb.chunks}), "kb_chunks": len(service.kb.chunks)}


@app.get("/v1/demo/customers", dependencies=[Depends(access)])
def demo_customers():
    if settings.demo_mode:
        service.refresh_if_stale()
    out = []
    for c in service.repo.list_customers():
        orders = service.repo.list_orders(c["id"], limit=100)
        city = service.repo.get_order(c["id"], orders[0]["id"])["shipping_city"] if orders else ""
        out.append({"id": c["id"], "name": c["name"], "tier": c["tier"], "member_since": c["member_since"],
                    "city": city, "orders": len(orders)})
    return out


@app.get("/v1/demo/customers/{customer_id}/orders", dependencies=[Depends(access)])
def demo_orders(customer_id: str):
    orders = []
    for o in service.repo.list_orders(customer_id, limit=20):
        full = service.repo.get_order(customer_id, o["id"])
        ship = full["shipment"]
        orders.append({
            "id": full["id"], "status": full["status"], "placed_at": full["placed_at"], "delivered_at": full["delivered_at"],
            "total": money(full["total"]), "payment_method": full["payment_method"],
            "items": [{"name": i["name"], "quantity": i["quantity"], "price": money(i["unit_price"]),
                       "policy": policy_label(i), "request": i["request_status"] and f"{i['request_resolution']} · {i['request_status']}"}
                      for i in full["items"]],
            "shipment": ship and {"carrier": ship["carrier"], "eta": ship["eta"], "last_location": ship["last_location"],
                                  "cancellation_requested": bool(ship["cancellation_requested"])},
        })
    return orders


@app.get("/v1/evals")
def eval_reports():
    names = {"scenarios": "scenarios_latest.md", "retrieval": "retrieval_latest.md", "models": "model_comparison.md"}
    return {k: (REPORTS_DIR / f).read_text(encoding="utf-8") if (REPORTS_DIR / f).exists() else None for k, f in names.items()}


# ---------------- customer ----------------
@app.post("/v1/conversations")
def start_conversation(customer_id: str = Depends(customer)):
    if settings.demo_mode:
        service.refresh_if_stale()
    return {"conversation_id": service.start_conversation(customer_id).id}


@app.post("/v1/conversations/{conversation_id}/messages", dependencies=[Depends(rate_limited)])
def send_message(conversation_id: str, body: MessageIn, customer_id: str = Depends(customer)):
    session = owned_session(conversation_id, customer_id)
    if session.turn >= settings.demo_turns_per_conversation:
        raise HTTPException(429, "This conversation reached its length limit. Start a new chat.")
    if service.repo.turns_today() >= settings.demo_daily_turn_cap:
        raise HTTPException(429, "The demo's daily limit has been reached. Please come back tomorrow.")
    return result_to_dict(service.send(conversation_id, body.text))


@app.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str, customer_id: str = Depends(customer)):
    owned_session(conversation_id, customer_id)
    return {"messages": service.repo.messages(conversation_id)}


# ---------------- support desk (human agents) ----------------
@app.get("/v1/admin/metrics", dependencies=[Depends(admin)])
def metrics():
    return service.metrics()


@app.get("/v1/admin/approvals", dependencies=[Depends(admin)])
def approvals():
    return service.pending_approvals()


@app.post("/v1/admin/approvals/{request_id}", dependencies=[Depends(admin)])
def decide(request_id: int, body: Decision):
    if not service.decide_return(request_id, body.approve, body.agent_name):
        raise HTTPException(409, "request is not pending approval")
    return {"request_id": request_id, "approved": body.approve}


@app.get("/v1/admin/tickets", dependencies=[Depends(admin)])
def tickets(status: str | None = "open"):
    return service.tickets(status)


@app.post("/v1/admin/tickets/{ticket_id}/resolve", dependencies=[Depends(admin)])
def resolve(ticket_id: int):
    if not service.resolve_ticket(ticket_id):
        raise HTTPException(404, "ticket not found")
    return {"ticket_id": ticket_id, "status": "resolved"}


@app.get("/v1/admin/conversations", dependencies=[Depends(admin)])
def conversations():
    return service.conversations()


@app.get("/v1/admin/conversations/{conversation_id}", dependencies=[Depends(admin)])
def conversation_trace(conversation_id: str):
    return service.transcript(conversation_id)


@app.get("/v1/admin/quota", dependencies=[Depends(admin)])
def quota():
    return llm.quota_status()


@app.post("/v1/admin/reset-demo", dependencies=[Depends(admin)])
def reset_demo():
    if not settings.demo_mode:
        raise HTTPException(403, "only available in demo mode")
    service.reset_demo_data()
    return {"status": "reset"}


# ---------------- web UI ----------------
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")
