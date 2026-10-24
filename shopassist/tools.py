"""The agent's tools. Design rules:
- Customer identity comes from the session (login), never from tool arguments.
- Tools return structured dicts (not prose) so the model can reason over fields.
- Errors are returned to the model as data, so it can recover (e.g. ask for the right order id).
- Write actions are two-phase: propose (confirmed=false) -> customer says yes in a later turn -> execute.
"""
from dataclasses import dataclass, field

from .config import money, today
from .db.repository import Repository
from .guardrails import is_confirmation
from .policies import (REFUND_START, REFUND_TIMELINES, RESOLUTIONS, RETURN_REASONS, cancellation_decision,
                       delay_status, needs_human_approval, policy_label, return_decision)
from .retrieval import KnowledgeBase

ACTION_TOOLS = {"request_return", "cancel_order", "escalate_to_human"}
SLA = {"urgent": "within 2 hours", "high": "within 4 hours", "normal": "within 24 hours"}
ESCALATION_REASONS = ("customer_request", "complaint", "delayed_or_lost_order", "refund_issue",
                      "exchange_request", "legal_or_safety", "other")


@dataclass
class ToolContext:
    repo: Repository
    kb: KnowledgeBase
    customer: dict
    conversation_id: str
    turn: int
    last_user_message: str
    pending: dict = field(default_factory=dict)      # action key -> {"turn", "summary"}; lives in the session
    citations: dict = field(default_factory=dict)    # "KB1" -> chunk, for this turn
    succeeded_actions: list = field(default_factory=list)
    ticket_id: int | None = None


DECLARATIONS = [
    {
        "name": "search_help_center",
        "description": "Search the store's real help-center pages (Amazon.in): returns, replacements, refunds and "
                       "refund timelines, cancellations, delivery and tracking, late or missing packages, damaged/"
                       "defective/wrong items, warranty, payments, Pay on Delivery, EMI, gift cards, Prime, account "
                       "security. Returns passages tagged KB1, KB2...; cite them as [KB1].",
        "parameters": {"type": "OBJECT", "required": ["query"], "properties": {
            "query": {"type": "STRING", "description": "A focused search query, e.g. 'refund time for UPI'."}}},
    },
    {
        "name": "list_orders",
        "description": "List this customer's recent orders, newest first, with status and item names.",
        "parameters": {"type": "OBJECT", "properties": {
            "status": {"type": "STRING", "enum": ["order_received", "preparing_for_shipment", "shipped",
                                                  "out_for_delivery", "delivered", "cancelled"],
                       "description": "Optional status filter."}}},
    },
    {
        "name": "get_order",
        "description": "Full details of one of this customer's orders: items (with item_id and each item's return "
                       "policy), payment method, dates, shipment and any existing return/replacement requests.",
        "parameters": {"type": "OBJECT", "required": ["order_id"], "properties": {"order_id": {"type": "INTEGER"}}},
    },
    {
        "name": "track_shipment",
        "description": "Live tracking for an order: courier, tracking number, estimated delivery, last location, "
                       "and whether it is late beyond the allowed grace period.",
        "parameters": {"type": "OBJECT", "required": ["order_id"], "properties": {"order_id": {"type": "INTEGER"}}},
    },
    {
        "name": "check_return_eligibility",
        "description": "Ask the policy engine what is possible for items in a delivered order (return for refund, "
                       "replacement, brand/service-centre support) given the reason. Call this before telling the "
                       "customer whether something can be returned or replaced.",
        "parameters": {"type": "OBJECT", "required": ["order_id", "reason"], "properties": {
            "order_id": {"type": "INTEGER"},
            "item_id": {"type": "INTEGER", "description": "Optional; omit to check every item in the order."},
            "reason": {"type": "STRING", "enum": list(RETURN_REASONS)},
            "resolution": {"type": "STRING", "enum": list(RESOLUTIONS), "description": "What the customer prefers, if known."}}},
    },
    {
        "name": "request_return",
        "description": "Create a return (refund) or replacement request for one item. TWO-STEP: call with "
                       "confirmed=false as soon as the customer wants it (this registers the request and returns the "
                       "summary to show them); call again with confirmed=true only after they explicitly agree in a "
                       "later message.",
        "parameters": {"type": "OBJECT", "required": ["order_id", "item_id", "reason", "resolution", "confirmed"], "properties": {
            "order_id": {"type": "INTEGER"},
            "item_id": {"type": "INTEGER"},
            "reason": {"type": "STRING", "enum": list(RETURN_REASONS)},
            "resolution": {"type": "STRING", "enum": list(RESOLUTIONS)},
            "confirmed": {"type": "BOOLEAN"}}},
    },
    {
        "name": "cancel_order",
        "description": "Cancel an order that hasn't shipped, or request cancellation of one that has shipped (the "
                       "courier is asked to stop it). TWO-STEP like request_return.",
        "parameters": {"type": "OBJECT", "required": ["order_id", "confirmed"], "properties": {
            "order_id": {"type": "INTEGER"},
            "confirmed": {"type": "BOOLEAN"}}},
    },
    {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human specialist by creating a support ticket.",
        "parameters": {"type": "OBJECT", "required": ["reason", "priority", "summary"], "properties": {
            "reason": {"type": "STRING", "enum": list(ESCALATION_REASONS)},
            "priority": {"type": "STRING", "enum": list(SLA)},
            "summary": {"type": "STRING", "description": "2-3 sentence handover note for the specialist: the problem, "
                                                         "what was already checked, and what the customer wants."},
            "order_id": {"type": "INTEGER", "description": "Optional related order."}}},
    },
]


class Tools:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, name, None) if name in {d["name"] for d in DECLARATIONS} else None
        if handler is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            for key in ("order_id", "item_id"):  # models sometimes send 1001.0 or "1001"
                if args.get(key) is not None:
                    args[key] = int(float(args[key]))
            return handler(**args)
        except TypeError as e:
            return {"error": f"Bad arguments for {name}: {e}"}
        except Exception as e:  # a failing tool must never crash the conversation
            return {"error": f"Internal error in {name} ({type(e).__name__}). Apologise and offer a specialist."}

    # ---------------- read-only ----------------
    def search_help_center(self, query: str) -> dict:
        results = []
        for hit in self.ctx.kb.search(query):
            cid = next((k for k, v in self.ctx.citations.items() if v["hash"] == hit["hash"]), None)
            if cid is None:
                cid = f"KB{len(self.ctx.citations) + 1}"
                self.ctx.citations[cid] = hit
            results.append({"id": cid, "page": hit["title"], "section": hit["section"], "text": hit["text"]})
        return {"results": results}

    def list_orders(self, status: str | None = None) -> dict:
        return {"orders": self.ctx.repo.list_orders(self.ctx.customer["id"], status)}

    def _order(self, order_id: int) -> dict | None:
        return self.ctx.repo.get_order(self.ctx.customer["id"], order_id)

    @staticmethod
    def _not_found(order_id: int) -> dict:
        return {"error": f"No order {order_id} on this customer's account."}

    def get_order(self, order_id: int) -> dict:
        order = self._order(order_id)
        if not order:
            return self._not_found(order_id)
        return {
            "order_id": order["id"], "status": order["status"], "payment_method": order["payment_method"],
            "total": money(order["total"]), "placed_at": order["placed_at"], "delivered_at": order["delivered_at"],
            "items": [{"item_id": i["item_id"], "name": i["name"], "quantity": i["quantity"], "price": money(i["unit_price"]),
                       "return_policy": policy_label(i), "warranty_months": i["warranty_months"],
                       "existing_request": i["request_status"] and f"{i['request_resolution']} ({i['request_status']})"}
                      for i in order["items"]],
            "shipment": order["shipment"] and {k: order["shipment"][k] for k in ("carrier", "tracking_number", "eta")},
        }

    def track_shipment(self, order_id: int) -> dict:
        order = self._order(order_id)
        if not order:
            return self._not_found(order_id)
        ship = order["shipment"]
        if not ship:
            return {"order_id": order_id, "status": order["status"],
                    "note": "Order was cancelled." if order["status"] == "cancelled" else "Not handed to a courier yet."}
        delay = delay_status(ship, order, today())
        out = {"order_id": order_id, "status": order["status"], "shipped_by": ship["shipped_by"], "carrier": ship["carrier"],
               "tracking_number": ship["tracking_number"], "estimated_delivery": ship["eta"],
               "last_location": ship["last_location"], "last_update": ship["last_update"],
               "delivered_at": order["delivered_at"], "cancellation_requested": bool(ship["cancellation_requested"]), **delay}
        if delay["past_grace_period"]:
            out["policy"] = (f"Late beyond the {delay['grace_days']}-day grace period for {ship['shipped_by'].replace('_', '-')} "
                             "shipments: apologise and offer a high-priority investigation (escalate_to_human, "
                             "reason delayed_or_lost_order).")
        elif delay["days_late"]:
            out["policy"] = (f"Slightly late but within the {delay['grace_days']}-day grace period: ask the customer to "
                             "wait a little longer (see the 'Shipment is Late' help page).")
        return out

    def check_return_eligibility(self, order_id: int, reason: str, item_id: int | None = None,
                                 resolution: str | None = None) -> dict:
        order = self._order(order_id)
        if not order:
            return self._not_found(order_id)
        items = [i for i in order["items"] if item_id is None or i["item_id"] == item_id]
        if not items:
            return {"error": f"Item {item_id} is not in order {order_id}.",
                    "valid_item_ids": [i["item_id"] for i in order["items"]]}
        results = []
        for item in items:
            d = return_decision(item, order, reason, resolution, today())
            details = {k: (money(v) if k == "refund_amount" else v) for k, v in d.details.items()}
            if d.allowed and needs_human_approval(d.details["refund_amount"]):
                details["note"] = "Refund above the auto-approval limit: a specialist reviews it first (usually within 24 hours)."
            results.append({"item_id": item["item_id"], "name": item["name"], "allowed": d.allowed,
                            "code": d.code, "explanation": d.message, **details})
        return {"order_id": order_id, "payment_method": order["payment_method"],
                "refund_timeline": f"{REFUND_START} Then {REFUND_TIMELINES[order['payment_method']]}.",
                "items": results,
                "next_step": "If the customer wants to go ahead, call request_return with confirmed=false now."}

    # ---------------- write actions ----------------
    def _confirm_gate(self, key: str, confirmed: bool, summary: str) -> dict | None:
        """Two-phase commit for agent actions. Returns a 'confirmation_required' payload, or None to proceed.
        Execution requires (1) a proposal in an EARLIER turn and (2) an affirmative latest user message,
        both checked in code, so the model cannot skip asking even if it is told to."""
        pending = self.ctx.pending.get(key)
        if confirmed and pending and pending["turn"] < self.ctx.turn and is_confirmation(self.ctx.last_user_message):
            del self.ctx.pending[key]
            return None
        self.ctx.pending[key] = {"turn": self.ctx.turn, "summary": summary}
        payload = {"status": "confirmation_required", "summary": summary,
                   "instruction": "Show this summary to the customer and ask them to confirm. It is NOT done yet."}
        if confirmed:
            payload["error"] = "Not executed: the customer has not explicitly confirmed this in a later message."
        return payload

    def request_return(self, order_id: int, item_id: int, reason: str, resolution: str = "refund",
                       confirmed: bool = False) -> dict:
        order = self._order(order_id)
        if not order:
            return self._not_found(order_id)
        item = next((i for i in order["items"] if i["item_id"] == item_id), None)
        if not item:
            return {"error": f"Item {item_id} is not in order {order_id}.",
                    "valid_item_ids": [i["item_id"] for i in order["items"]]}

        # Re-checked at execution time too: state (stock, existing requests) may change between turns.
        d = return_decision(item, order, reason, resolution, today())
        if not d.allowed:
            return {"status": "not_allowed", "code": d.code, "explanation": d.message}

        final_resolution, refund = d.details["resolution"], d.details["refund_amount"]
        approval = needs_human_approval(refund)
        if final_resolution == "replacement":
            summary = (f"Free replacement of '{item['name']}' from order #{order_id} (reason: {reason}). The item is "
                       "picked up and the replacement ships by standard delivery.")
        else:
            summary = (f"Return '{item['name']}' from order #{order_id} (reason: {reason}) for a refund of {money(refund)}. "
                       f"{REFUND_START} Then {REFUND_TIMELINES[order['payment_method']]}.")
        if final_resolution != resolution:
            summary += f" Note: {d.message}"
        if approval:
            summary += " Because the refund is above the auto-approval limit, a specialist reviews it before pickup (usually within 24 hours)."

        gate = self._confirm_gate(f"return:{order_id}:{item_id}", confirmed, summary)
        if gate:
            return gate
        status = "pending_approval" if approval else "pickup_scheduled"
        req = self.ctx.repo.create_return(order_id, item_id, reason, final_resolution, status, refund)
        self.ctx.succeeded_actions.append("request_return")
        return {"status": "request_created" if req["created"] else "request_already_exists", "request_id": req["id"],
                "resolution": req["resolution"], "request_status": req["status"],
                "refund_amount": money(req["refund_amount"]) if req["resolution"] == "refund" else None,
                "next_step": "A specialist reviews the refund, then pickup is scheduled." if req["status"] == "pending_approval"
                else "Pickup will be scheduled; keep the item in its original condition with tags, box and accessories."}

    def cancel_order(self, order_id: int, confirmed: bool = False) -> dict:
        order = self._order(order_id)
        if not order:
            return self._not_found(order_id)
        d = cancellation_decision(order)
        if not d.allowed:
            return {"status": "not_allowed", "code": d.code, "explanation": d.message}
        items = ", ".join(i["name"] for i in order["items"])
        verb = "Cancel" if d.code == "cancellable" else "Request cancellation of"
        gate = self._confirm_gate(f"cancel:{order_id}", confirmed, f"{verb} order #{order_id} ({items}). {d.message}")
        if gate:
            return gate
        if d.code == "cancellable":
            ok = self.ctx.repo.cancel_order(self.ctx.customer["id"], order_id)
        else:
            ok = self.ctx.repo.request_cancellation(self.ctx.customer["id"], order_id)
        if not ok:
            return {"status": "failed", "explanation": "The order's status changed; please check it again."}
        self.ctx.succeeded_actions.append("cancel_order")
        return {"status": "cancelled" if d.code == "cancellable" else "cancellation_requested",
                "order_id": order_id, "details": d.message}

    def escalate_to_human(self, reason: str, priority: str, summary: str, order_id: int | None = None) -> dict:
        if self.ctx.ticket_id:  # one ticket per conversation; repeated calls are idempotent
            return {"status": "ticket_already_open", "ticket_id": self.ctx.ticket_id}
        priority = priority if priority in SLA else "normal"
        if order_id is not None and not self._order(order_id):
            order_id = None
        self.ctx.ticket_id = self.ctx.repo.create_ticket(
            self.ctx.customer["id"], self.ctx.conversation_id, order_id, priority, reason, summary)
        self.ctx.succeeded_actions.append("escalate_to_human")
        return {"status": "ticket_created", "ticket_id": self.ctx.ticket_id, "priority": priority,
                "expected_response": SLA[priority]}
