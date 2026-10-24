"""Data-access layer. Every customer-facing query takes customer_id and filters on it in SQL, so the
agent physically cannot read another customer's data, whatever the model is tricked into asking.
All SQL is parameterised (no string formatting), which rules out SQL injection through tool args.
"""
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or settings.db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"{self.db_path} not found. Run: python -m shopassist.db.seed")

    @contextmanager
    def _conn(self):
        # One short-lived connection per operation: safe across threads (FastAPI/Streamlit) and processes.
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _all(self, sql: str, params=()) -> list[dict]:
        with self._conn() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # ---------- customers ----------
    def get_customer(self, customer_id: str) -> dict | None:
        return self._one("SELECT * FROM customers WHERE id = ?", (customer_id,))

    def list_customers(self) -> list[dict]:
        return self._all("SELECT * FROM customers ORDER BY id")

    # ---------- orders (always scoped to the customer) ----------
    def list_orders(self, customer_id: str, status: str | None = None, limit: int = 10) -> list[dict]:
        sql = """SELECT o.id, o.status, o.total, o.placed_at, o.delivered_at, o.payment_method,
                        GROUP_CONCAT(p.name, ', ') AS items
                 FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id
                 WHERE o.customer_id = ? AND (? IS NULL OR o.status = ?)
                 GROUP BY o.id ORDER BY o.placed_at DESC, o.id DESC LIMIT ?"""
        return self._all(sql, (customer_id, status, status, limit))

    def get_order(self, customer_id: str, order_id: int) -> dict | None:
        """None when the order doesn't exist OR belongs to someone else: same answer either way,
        so the agent can't be used to probe which order ids exist."""
        order = self._one("SELECT * FROM orders WHERE id = ? AND customer_id = ?", (order_id, customer_id))
        if not order:
            return None
        order["items"] = self._all(
            """SELECT oi.id AS item_id, oi.quantity, oi.unit_price, p.id AS product_id, p.name, p.category,
                      p.return_policy, p.window_days, p.warranty_months, p.in_stock,
                      r.id AS request_id, r.status AS request_status, r.resolution AS request_resolution
               FROM order_items oi JOIN products p ON p.id = oi.product_id
               LEFT JOIN return_requests r ON r.order_item_id = oi.id
               WHERE oi.order_id = ? ORDER BY oi.id""", (order_id,))
        order["shipment"] = self._one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
        return order

    def cancel_order(self, customer_id: str, order_id: int) -> bool:
        with self._conn() as con:
            cur = con.execute(
                "UPDATE orders SET status = 'cancelled' WHERE id = ? AND customer_id = ? "
                "AND status IN ('order_received', 'preparing_for_shipment')", (order_id, customer_id))
            return cur.rowcount == 1

    def request_cancellation(self, customer_id: str, order_id: int) -> bool:
        """Shipped orders can't be cancelled directly; flag the shipment so the courier is asked to stop it."""
        with self._conn() as con:
            cur = con.execute(
                "UPDATE shipments SET cancellation_requested = 1 WHERE order_id = (SELECT id FROM orders "
                "WHERE id = ? AND customer_id = ? AND status IN ('shipped', 'out_for_delivery'))", (order_id, customer_id))
            return cur.rowcount == 1

    def create_return(self, order_id: int, item_id: int, reason: str, resolution: str, status: str,
                      refund_amount: float) -> dict:
        """Idempotent: UNIQUE(order_item_id) means a retried tool call returns the existing request."""
        with self._conn() as con:
            try:
                cur = con.execute(
                    "INSERT INTO return_requests (order_id, order_item_id, reason, resolution, status, refund_amount, "
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (order_id, item_id, reason, resolution, status, refund_amount, now_iso()))
                request_id, created = cur.lastrowid, True
            except sqlite3.IntegrityError:
                request_id, created = con.execute(
                    "SELECT id FROM return_requests WHERE order_item_id = ?", (item_id,)).fetchone()[0], False
        return {**self._one("SELECT * FROM return_requests WHERE id = ?", (request_id,)), "created": created}

    def create_ticket(self, customer_id: str, conversation_id: str | None, order_id: int | None,
                      priority: str, reason: str, summary: str) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO tickets (customer_id, conversation_id, order_id, priority, reason, summary, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (customer_id, conversation_id, order_id, priority, reason, summary, now_iso()))
            if conversation_id:
                con.execute("UPDATE conversations SET escalated = 1 WHERE id = ?", (conversation_id,))
            return cur.lastrowid

    # ---------- human-agent (admin) side ----------
    def pending_approvals(self) -> list[dict]:
        return self._all(
            """SELECT r.*, o.customer_id, c.name AS customer_name, p.name AS product, o.payment_method
               FROM return_requests r JOIN orders o ON o.id = r.order_id JOIN customers c ON c.id = o.customer_id
               JOIN order_items oi ON oi.id = r.order_item_id JOIN products p ON p.id = oi.product_id
               WHERE r.status = 'pending_approval' ORDER BY r.created_at""")

    def decide_return(self, return_id: int, approve: bool, decided_by: str) -> bool:
        with self._conn() as con:
            cur = con.execute(
                "UPDATE return_requests SET status = ?, decided_by = ? WHERE id = ? AND status = 'pending_approval'",
                ("pickup_scheduled" if approve else "rejected", decided_by, return_id))
            return cur.rowcount == 1

    def tickets(self, status: str | None = "open") -> list[dict]:
        return self._all(
            """SELECT t.*, c.name AS customer_name FROM tickets t JOIN customers c ON c.id = t.customer_id
               WHERE (? IS NULL OR t.status = ?)
               ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                        t.created_at""", (status, status))

    def resolve_ticket(self, ticket_id: int) -> bool:
        with self._conn() as con:
            return con.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,)).rowcount == 1

    def returns_for_customer(self, customer_id: str) -> list[dict]:
        return self._all(
            """SELECT r.*, p.name AS product FROM return_requests r JOIN orders o ON o.id = r.order_id
               JOIN order_items oi ON oi.id = r.order_item_id JOIN products p ON p.id = oi.product_id
               WHERE o.customer_id = ? ORDER BY r.id""", (customer_id,))

    # ---------- conversations, messages, traces ----------
    def create_conversation(self, conversation_id: str, customer_id: str):
        with self._conn() as con:
            con.execute("INSERT INTO conversations VALUES (?,?,?,0)", (conversation_id, customer_id, now_iso()))

    def add_message(self, conversation_id: str, role: str, content: str):
        with self._conn() as con:
            con.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                        (conversation_id, role, content, now_iso()))

    def save_trace(self, trace: dict):
        with self._conn() as con:
            con.execute(
                "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trace["id"], trace["conversation_id"], trace["turn"], now_iso(), trace["latency_ms"],
                 trace["llm_calls"], trace["tool_calls"], trace["tool_errors"], trace["input_tokens"],
                 trace["output_tokens"], json.dumps(trace["flags"]), json.dumps(trace["events"], default=str)))

    def seeded_on(self) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = 'seeded_on'")
        return row["value"] if row else None

    def turns_today(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM traces WHERE created_at >= ?", (now_iso()[:10],))["n"]

    def conversations(self, limit: int = 50) -> list[dict]:
        return self._all(
            """SELECT cv.*, c.name AS customer_name, COUNT(m.id) AS messages
               FROM conversations cv JOIN customers c ON c.id = cv.customer_id
               LEFT JOIN messages m ON m.conversation_id = cv.id
               GROUP BY cv.id HAVING COUNT(m.id) > 0 ORDER BY cv.started_at DESC LIMIT ?""", (limit,))

    def messages(self, conversation_id: str) -> list[dict]:
        return self._all("SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                         (conversation_id,))

    def traces(self, conversation_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM traces WHERE conversation_id = ? ORDER BY turn", (conversation_id,))
        for r in rows:
            r["flags"], r["events"] = json.loads(r["flags"]), json.loads(r["events"])
        return rows

    def metrics(self) -> dict:
        m = self._one(
            """SELECT COUNT(*) AS turns, COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(tool_calls), 0) AS tool_calls, COALESCE(SUM(tool_errors), 0) AS tool_errors,
                      COALESCE(SUM(flags != '[]'), 0) AS flagged_turns
               FROM traces""")
        latencies = [r["latency_ms"] for r in self._all("SELECT latency_ms FROM traces ORDER BY latency_ms")]
        m["p95_latency_ms"] = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else 0  # nearest-rank
        conv = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(escalated), 0) AS escalated FROM conversations "
                         "WHERE id IN (SELECT conversation_id FROM messages)")  # ignore chats opened but never used
        m["conversations"] = conv["n"]
        m["escalated"] = conv["escalated"]
        m["containment_rate"] = 1 - conv["escalated"] / conv["n"] if conv["n"] else None
        m["open_tickets"] = self._one("SELECT COUNT(*) AS n FROM tickets WHERE status = 'open'")["n"]
        m["pending_approvals"] = self._one("SELECT COUNT(*) AS n FROM return_requests WHERE status = 'pending_approval'")["n"]
        m["returns_created"] = self._one("SELECT COUNT(*) AS n FROM return_requests")["n"]
        m["est_cost_usd"] = (m["input_tokens"] * settings.price_in_per_m + m["output_tokens"] * settings.price_out_per_m) / 1e6
        return m
