-- Operational data (what a real order system holds) + support data (what the agent creates).
-- Column choices mirror Amazon.in's public policies: each product carries a return-policy TYPE and a
-- window (e.g. "10 Days Returnable", "7 Days Replacement"), shipments are Amazon- or third-party-
-- shipped (different delay rules), and replacements depend on stock.
-- Plain SQL: runs on SQLite, ports to Postgres with minimal changes.

CREATE TABLE customers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    phone         TEXT,
    tier          TEXT NOT NULL DEFAULT 'standard' CHECK (tier IN ('standard', 'prime')),
    member_since  TEXT NOT NULL
);

CREATE TABLE products (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    price            REAL NOT NULL,
    return_policy    TEXT NOT NULL CHECK (return_policy IN ('returnable', 'returns_and_exchange', 'replacement_only',
                                         'brand_replacement', 'service_centre_replacement', 'non_returnable')),
    window_days      INTEGER NOT NULL DEFAULT 0,
    warranty_months  INTEGER NOT NULL DEFAULT 0,
    in_stock         INTEGER NOT NULL DEFAULT 0     -- replacements need the exact item in stock
);

CREATE TABLE orders (
    id               INTEGER PRIMARY KEY,
    customer_id      TEXT NOT NULL REFERENCES customers(id),
    status           TEXT NOT NULL CHECK (status IN ('order_received', 'preparing_for_shipment', 'shipped',
                                                     'out_for_delivery', 'delivered', 'cancelled')),
    payment_method   TEXT NOT NULL CHECK (payment_method IN ('card', 'upi', 'netbanking', 'amazon_pay', 'pay_on_delivery')),
    total            REAL NOT NULL,
    placed_at        TEXT NOT NULL,
    delivered_at     TEXT,
    shipping_city    TEXT NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);

CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  TEXT NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    unit_price  REAL NOT NULL
);
CREATE INDEX idx_items_order ON order_items(order_id);

CREATE TABLE shipments (
    order_id                INTEGER PRIMARY KEY REFERENCES orders(id),
    shipped_by              TEXT NOT NULL CHECK (shipped_by IN ('amazon', 'third_party')),
    carrier                 TEXT NOT NULL,
    tracking_number         TEXT NOT NULL,
    eta                     TEXT,
    last_location           TEXT,
    last_update             TEXT,
    cancellation_requested  INTEGER NOT NULL DEFAULT 0
);

-- Support-side tables written by the agent / human specialists
CREATE TABLE return_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    order_item_id   INTEGER NOT NULL REFERENCES order_items(id),
    reason          TEXT NOT NULL,
    resolution      TEXT NOT NULL CHECK (resolution IN ('refund', 'replacement')),
    status          TEXT NOT NULL CHECK (status IN ('pending_approval', 'pickup_scheduled', 'rejected')),
    refund_amount   REAL NOT NULL,
    created_at      TEXT NOT NULL,
    decided_by      TEXT,
    UNIQUE (order_item_id)   -- idempotency: one request per item, even if a tool call is retried
);

CREATE TABLE tickets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id      TEXT NOT NULL REFERENCES customers(id),
    conversation_id  TEXT,
    order_id         INTEGER,
    priority         TEXT NOT NULL CHECK (priority IN ('normal', 'high', 'urgent')),
    reason           TEXT NOT NULL,
    summary          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at       TEXT NOT NULL
);

CREATE TABLE conversations (
    id           TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    started_at   TEXT NOT NULL,
    escalated    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id),
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,          -- secrets redacted + PII masked before storage
    created_at       TEXT NOT NULL
);

-- Bookkeeping (e.g. the date the demo data was generated, so it can be refreshed daily)
CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- One row per agent turn: what the agent did, how long it took, what it cost, which guardrails fired.
CREATE TABLE traces (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    turn             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    latency_ms       INTEGER NOT NULL,
    llm_calls        INTEGER NOT NULL,
    tool_calls       INTEGER NOT NULL,
    tool_errors      INTEGER NOT NULL,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    flags            TEXT NOT NULL,          -- JSON list of guardrail flags
    events           TEXT NOT NULL           -- JSON list of llm/tool events
);
