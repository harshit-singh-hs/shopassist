"""The policy engine encodes rules from the crawled Amazon.in pages. If a refresh of the knowledge base
changes those pages, these tests fail, so code and published policy can't silently drift apart."""
import re

import pytest

from shopassist.config import settings


def page(name: str) -> str:
    if not any(settings.kb_dir.glob("*.md")):
        pytest.skip("knowledge base not fetched (run: python -m scripts.fetch_kb)")
    path = settings.kb_dir / f"{name}.md"
    assert path.exists(), f"critical page {name}.md is missing from the knowledge base"  # fail, don't skip
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def test_return_policy_types_exist_in_source():
    text = page("amazon-in-returns-policy")
    for label in ["10/30 Days Returnable", "7/10 Days Replacement", "Replacement by Brand",
                  "Service Centre Replacement", "10 Day Free Returns & Exchange", "Non-Returnable"]:
        assert label in text, label


def test_non_returnable_damage_window_is_5_days():
    assert "within 5 days from the delivery" in page("amazon-in-returns-policy")


def test_out_of_stock_replacement_becomes_refund():
    assert "a refund would be issued to you instead of a Replacement" in page("amazon-in-returns-policy")


def test_refund_timelines():
    text = page("amazon-in-refund-policy")
    assert "Up to 4 hours" in text and "Up to 5 Working Days" in text and "within 13 days" in text


def test_cancellation_statuses():
    text = page("cancel-items-and-orders")
    assert "Preparing for Shipment" in text and "Order Received" in text and "Request Cancellation" in text


def test_delay_grace_periods():
    text = page("tracking-your-package")
    assert "Wait for one more day" in text and "Wait for five more days" in text
