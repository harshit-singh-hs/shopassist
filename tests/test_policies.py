from datetime import date, timedelta

import pytest

from shopassist import policies
from shopassist.config import settings

TODAY = date(2026, 1, 20)


def make(policy="returnable", window=10, delivered_days_ago=3, in_stock=5, warranty=0, price=1000.0, status="delivered"):
    item = {"return_policy": policy, "window_days": window, "warranty_months": warranty, "in_stock": in_stock,
            "unit_price": price, "quantity": 1, "request_status": None}
    order = {"status": status, "delivered_at": (TODAY - timedelta(days=delivered_days_ago)).isoformat()}
    return item, order


def decide(reason, resolution=None, **kw):
    item, order = make(**kw)
    return policies.return_decision(item, order, reason, resolution, TODAY)


def test_returnable_change_of_mind_within_window_refunds():
    d = decide("no_longer_needed")
    assert d.allowed and d.details["resolution"] == "refund" and d.details["refund_amount"] == 1000


def test_window_expired_mentions_warranty_for_defects():
    d = decide("defective", delivered_days_ago=15, warranty=12)
    assert not d.allowed and d.code == "window_expired" and "warranty" in d.message


def test_replacement_only_rejects_change_of_mind():
    d = decide("changed_mind", policy="replacement_only", window=7)
    assert not d.allowed and d.code == "replacement_only"


def test_replacement_only_damaged_in_stock_gets_replacement():
    d = decide("damaged", policy="replacement_only", window=7)
    assert d.allowed and d.details["resolution"] == "replacement" and d.details["refund_amount"] == 0


def test_out_of_stock_replacement_becomes_refund():
    # Amazon.in: "if the seller does not have the exact same product in stock, a refund would be issued"
    d = decide("damaged", resolution="replacement", policy="brand_replacement", in_stock=0)
    assert d.allowed and d.details["resolution"] == "refund" and "out of stock" in d.message


def test_brand_and_service_centre_handle_defects():
    assert decide("defective", policy="brand_replacement").code == "brand_handles_defects"
    assert decide("defective", policy="service_centre_replacement", window=7).code == "service_centre_handles_defects"
    assert decide("damaged", policy="service_centre_replacement", window=7).allowed


@pytest.mark.parametrize("days, reason, allowed, code", [
    (3, "damaged", True, "eligible"),
    (5, "wrong_item", True, "eligible"),
    (6, "damaged", False, "issue_reported_too_late"),
    (1, "changed_mind", False, "non_returnable"),
])
def test_non_returnable_rules(days, reason, allowed, code):
    d = decide(reason, policy="non_returnable", window=0, delivered_days_ago=days)
    assert (d.allowed, d.code) == (allowed, code)


def test_not_delivered_and_already_requested():
    assert decide("damaged", status="shipped").code == "not_delivered"
    item, order = make()
    item["request_status"] = "pickup_scheduled"
    assert policies.return_decision(item, order, "damaged", None, TODAY).code == "already_requested"


def test_unknown_reason_rejected():
    assert decide("free_money").code == "unknown_reason"


@pytest.mark.parametrize("status, allowed, code", [
    ("order_received", True, "cancellable"),
    ("preparing_for_shipment", True, "cancellable"),
    ("shipped", True, "request_cancellation"),
    ("out_for_delivery", True, "request_cancellation"),
    ("delivered", False, "delivered"),
    ("cancelled", False, "status_cancelled"),
])
def test_cancellation(status, allowed, code):
    d = policies.cancellation_decision({"status": status, "payment_method": "card", "total": 500})
    assert (d.allowed, d.code) == (allowed, code)


def test_pay_on_delivery_cancellation_has_nothing_to_refund():
    d = policies.cancellation_decision({"status": "order_received", "payment_method": "pay_on_delivery", "total": 899})
    assert "Nothing to refund" in d.message


@pytest.mark.parametrize("shipped_by, days_late, past", [
    ("amazon", 1, False), ("amazon", 2, True), ("third_party", 5, False), ("third_party", 6, True),
])
def test_delay_grace_periods(shipped_by, days_late, past):
    ship = {"eta": (TODAY - timedelta(days=days_late)).isoformat(), "shipped_by": shipped_by}
    assert policies.delay_status(ship, {"status": "shipped"}, TODAY)["past_grace_period"] is past


def test_human_approval_threshold():
    assert not policies.needs_human_approval(settings.refund_approval_threshold)
    assert policies.needs_human_approval(settings.refund_approval_threshold + 1)
