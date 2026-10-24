"""Business rules as plain, deterministic, unit-tested functions, encoded from Amazon.in's public
help pages (knowledge_base/amazon_in/, see the source line on each rule).

The LLM never decides whether something is allowed. It *asks* through a tool; this module
*decides*. That is why prompt injection cannot talk the system into an out-of-policy refund.
tests/test_kb_consistency.py checks that these numbers still match the crawled pages, so a policy
change on the source site fails CI instead of silently diverging.
"""
from dataclasses import dataclass, field
from datetime import date

from .config import settings

# Source: "Amazon.in Returns Policy" > "Return Policy Types with Descriptions".
POLICY_LABELS = {
    "returnable": "{w} Days Returnable",
    "returns_and_exchange": "{w} Day Free Returns & Exchange",
    "replacement_only": "{w} Days Replacement",
    "brand_replacement": "{w} Days Replacement by Brand",
    "service_centre_replacement": "{w} Days Service Centre Replacement",
    "non_returnable": "Non-Returnable",
}
ISSUE_REASONS = ("damaged", "defective", "wrong_item", "missing_parts")
CHANGE_OF_MIND_REASONS = ("changed_mind", "size_or_fit", "ordered_by_mistake", "no_longer_needed", "better_price")
RETURN_REASONS = ISSUE_REASONS + CHANGE_OF_MIND_REASONS
RESOLUTIONS = ("refund", "replacement")

# Source: Returns Policy note - "If you've received a non-returnable product in a damaged/defective
# condition, you can contact us within 5 days from the delivery of the product."
NON_RETURNABLE_ISSUE_DAYS = 5

# Source: "Cancel Items and Orders" - direct cancellation while Order Received / Preparing for
# Shipment; once shipped, the customer can only *request* cancellation (or refuse delivery).
CANCELLABLE_STATUSES = {"order_received", "preparing_for_shipment"}
REQUEST_CANCELLATION_STATUSES = {"shipped", "out_for_delivery"}

# Source: "Amazon.in Refund Policy" > "Refund Timelines" (after the return is received by Amazon).
REFUND_TIMELINES = {
    "amazon_pay": "up to 4 hours, as Amazon Pay balance",
    "card": "up to 5 working days, to the original credit/debit card",
    "netbanking": "up to 5 working days, to the original bank account",
    "upi": "up to 5 working days, to the UPI-linked bank account",
    "pay_on_delivery": "up to 5 working days by NEFT to your bank account, or up to 4 hours as Amazon Pay balance",
}
REFUND_START = "The refund is initiated once the returned item reaches Amazon (processed within 13 days of pickup)."

# Source: "Tracking your package" > "If Your Package is Delayed" - wait one more day for
# Amazon-shipped packages, five more days for third-party couriers.
DELAY_GRACE_DAYS = {"amazon": 1, "third_party": 5}


@dataclass
class Decision:
    allowed: bool
    code: str
    message: str
    details: dict = field(default_factory=dict)


def policy_label(item: dict) -> str:
    return POLICY_LABELS[item["return_policy"]].format(w=item["window_days"])


def _resolution(item: dict, reason: str, wanted: str | None) -> tuple[str, str]:
    """Replacement needs an issue reason and the exact item in stock; otherwise a refund is issued
    (Returns Policy: 'if the seller does not have the exact same product in stock, a refund would be
    issued to you instead of a Replacement')."""
    if wanted == "replacement" and reason in ISSUE_REASONS:
        if item["in_stock"] > 0:
            return "replacement", "A free replacement will be shipped after pickup."
        return "refund", "This exact item is out of stock, so a refund will be issued instead of a replacement."
    if wanted == "replacement":
        return "refund", "Replacements are only for damaged, defective, wrong or incomplete items, so this will be a refund."
    return "refund", "A refund will be issued after pickup."


def return_decision(item: dict, order: dict, reason: str, resolution: str | None, on: date) -> Decision:
    """item needs: return_policy, window_days, warranty_months, in_stock, unit_price, quantity, request_status."""
    label = policy_label(item)
    if order["status"] != "delivered":
        return Decision(False, "not_delivered", "The item has not been delivered yet, so a return can't be started. "
                        "Before it ships the order can be cancelled; after it ships, cancellation can be requested.")
    if item.get("request_status"):
        return Decision(False, "already_requested",
                        f"A return/replacement request already exists for this item (status: {item['request_status']}). "
                        "A free replacement can only be created once per item.")
    if reason not in RETURN_REASONS:
        return Decision(False, "unknown_reason", f"Reason must be one of {', '.join(RETURN_REASONS)}.")

    days = (on - date.fromisoformat(order["delivered_at"])).days
    issue = reason in ISSUE_REASONS
    warranty_left = item["warranty_months"] * 30 - days
    warranty_note = (f" It is still under the manufacturer's warranty (about {warranty_left} days left), so the brand's "
                     "service centre can help.") if issue and warranty_left > 0 else ""
    policy, window = item["return_policy"], item["window_days"]

    if policy == "non_returnable":
        if issue and days <= NON_RETURNABLE_ISSUE_DAYS:
            res, note = _resolution(item, reason, resolution or "refund")
            return _allowed(item, res, note, days, NON_RETURNABLE_ISSUE_DAYS, label,
                            "Non-returnable item reported damaged/defective/wrong within 5 days of delivery.")
        if issue:
            return Decision(False, "issue_reported_too_late", f"This item is {label}. Damaged or defective non-returnable "
                            f"items must be reported within {NON_RETURNABLE_ISSUE_DAYS} days of delivery; it was delivered "
                            f"{days} days ago.{warranty_note}", {"policy": label, "days_since_delivery": days})
        return Decision(False, "non_returnable", f"This item is {label}, so it can't be returned for a change of mind.",
                        {"policy": label})

    if days > window:
        return Decision(False, "window_expired", f"This item's policy is '{label}' and it was delivered {days} days ago, "
                        f"so the window closed {days - window} day(s) ago.{warranty_note}",
                        {"policy": label, "days_since_delivery": days, "warranty_days_left": max(warranty_left, 0)})

    if policy in ("returnable", "returns_and_exchange"):
        res, note = _resolution(item, reason, resolution)
        return _allowed(item, res, note, days, window, label, f"Within the '{label}' window.")

    if not issue:  # the three replacement-type policies don't cover change of mind
        return Decision(False, "replacement_only", f"This item's policy is '{label}': it can only be replaced if it "
                        "arrived damaged, defective, wrong or with missing parts, not returned for a change of mind.",
                        {"policy": label, "days_left": window - days})

    if reason == "defective" and policy == "brand_replacement":
        return Decision(False, "brand_handles_defects", f"For '{label}' items, defects are handled by the brand: they "
                        "provide on-call support followed by a technician visit, and the resolution is based on the "
                        "technician's report.", {"policy": label})
    if reason == "defective" and policy == "service_centre_replacement":
        return Decision(False, "service_centre_handles_defects", f"For '{label}' items, defects are handled by the "
                        "brand's authorised service centre, which decides on repair, replacement or refund.",
                        {"policy": label})

    # Replacement-type policies: replacement when in stock; a refund only when it can't be replaced.
    res, note = _resolution(item, reason, "replacement")
    if resolution == "refund" and res == "replacement":
        note += " (This policy offers a replacement; a refund is only issued if the item is out of stock.)"
    return _allowed(item, res, note, days, window, label, f"Within the '{label}' window.")


def _allowed(item: dict, resolution: str, note: str, days: int, window: int, label: str, why: str) -> Decision:
    refund = item["unit_price"] * item["quantity"] if resolution == "refund" else 0.0
    return Decision(True, "eligible", f"{why} {note}", {
        "policy": label, "resolution": resolution, "refund_amount": refund,
        "days_since_delivery": days, "days_left": window - days,
    })


def needs_human_approval(refund_amount: float) -> bool:
    """Internal risk control (not a customer-facing policy): large refunds get a human review."""
    return refund_amount > settings.refund_approval_threshold


def cancellation_decision(order: dict) -> Decision:
    status = order["status"]
    if status in CANCELLABLE_STATUSES:
        refund = ("Nothing to refund (Pay on Delivery)." if order["payment_method"] == "pay_on_delivery"
                  else f"The refund is processed immediately: {REFUND_TIMELINES[order['payment_method']]}.")
        return Decision(True, "cancellable", f"The order hasn't shipped, so it can be cancelled now. {refund}")
    if status in REQUEST_CANCELLATION_STATUSES:
        return Decision(True, "request_cancellation",
                        "The order has already shipped, so it can't be cancelled directly. A cancellation request asks "
                        "the courier to stop the delivery; if that isn't possible, the customer can refuse the package "
                        "at delivery and the refund is issued once it is returned.")
    if status == "delivered":
        return Decision(False, "delivered", "The order was already delivered; check whether the item can be returned instead.")
    return Decision(False, f"status_{status}", f"An order with status '{status}' can't be cancelled.")


def delay_status(shipment: dict | None, order: dict, on: date) -> dict:
    if not shipment or not shipment.get("eta") or order["status"] not in ("shipped", "out_for_delivery"):
        return {"days_late": 0, "past_grace_period": False}
    late = max(0, (on - date.fromisoformat(shipment["eta"])).days)
    grace = DELAY_GRACE_DAYS[shipment["shipped_by"]]
    return {"days_late": late, "grace_days": grace, "past_grace_period": late > grace}
