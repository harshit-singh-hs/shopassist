from shopassist.tools import ToolContext, Tools


def tools_for(repo, kb, customer_id="C001", turn=1, message="", pending=None):
    ctx = ToolContext(repo, kb, repo.get_customer(customer_id), "conv-test", turn, message,
                      pending=pending if pending is not None else {})
    return Tools(ctx)


def test_orders_are_scoped_to_the_logged_in_customer(repo, kb):
    t = tools_for(repo, kb, "C001")
    assert "error" in t.call("get_order", {"order_id": 1005})            # Sarah's order
    assert "error" in t.call("track_shipment", {"order_id": 1005})
    assert t.call("get_order", {"order_id": 1001})["order_id"] == 1001


def test_float_ids_from_the_model_are_coerced(repo, kb):
    assert tools_for(repo, kb).call("get_order", {"order_id": 1001.0})["order_id"] == 1001


def test_return_requires_proposal_in_an_earlier_turn(repo, kb):
    pending = {}
    args = {"order_id": 1001, "item_id": 2, "reason": "no_longer_needed", "resolution": "refund"}
    # Model tries to execute immediately: refused, but registered as a proposal.
    out = tools_for(repo, kb, turn=1, message="return my case", pending=pending).call(
        "request_return", {**args, "confirmed": True})
    assert out["status"] == "confirmation_required" and "error" in out
    assert repo.returns_for_customer("C001") == []
    # Next turn, customer says yes -> executes.
    out = tools_for(repo, kb, turn=2, message="yes please", pending=pending).call("request_return", {**args, "confirmed": True})
    assert out["status"] == "request_created" and out["request_status"] == "pickup_scheduled"
    assert len(repo.returns_for_customer("C001")) == 1


def test_return_not_executed_when_customer_declines(repo, kb):
    pending = {}
    args = {"order_id": 1001, "item_id": 2, "reason": "no_longer_needed", "resolution": "refund"}
    tools_for(repo, kb, turn=1, pending=pending).call("request_return", {**args, "confirmed": False})
    out = tools_for(repo, kb, turn=2, message="no, wait", pending=pending).call("request_return", {**args, "confirmed": True})
    assert out["status"] == "confirmation_required" and repo.returns_for_customer("C001") == []


def test_policy_enforced_even_if_model_insists(repo, kb):
    # Earbuds are "7 Days Replacement": change of mind is not returnable, whatever the model asks.
    out = tools_for(repo, kb).call("request_return", {"order_id": 1001, "item_id": 1, "reason": "changed_mind",
                                                     "resolution": "refund", "confirmed": True})
    assert out["status"] == "not_allowed" and out["code"] == "replacement_only"


def test_high_value_refund_goes_to_human_approval(repo, kb):
    pending = {}
    chair = repo.get_order("C002", 1005)["items"][0]["item_id"]
    args = {"order_id": 1005, "item_id": chair, "reason": "changed_mind", "resolution": "refund"}
    tools_for(repo, kb, "C002", turn=1, pending=pending).call("request_return", {**args, "confirmed": False})
    out = tools_for(repo, kb, "C002", turn=2, message="yes", pending=pending).call("request_return", {**args, "confirmed": True})
    assert out["request_status"] == "pending_approval"
    assert repo.pending_approvals()[0]["order_id"] == 1005


def test_return_requests_are_idempotent(repo):
    first = repo.create_return(1001, 2, "no_longer_needed", "refund", "pickup_scheduled", 399)
    again = repo.create_return(1001, 2, "no_longer_needed", "refund", "pickup_scheduled", 399)
    assert first["created"] and not again["created"] and first["id"] == again["id"]


def test_cancel_before_and_after_shipping(repo, kb):
    pending = {}
    tools_for(repo, kb, turn=1, pending=pending).call("cancel_order", {"order_id": 1003, "confirmed": False})
    out = tools_for(repo, kb, turn=2, message="yes cancel it", pending=pending).call("cancel_order", {"order_id": 1003, "confirmed": True})
    assert out["status"] == "cancelled" and repo.get_order("C001", 1003)["status"] == "cancelled"

    pending = {}
    tools_for(repo, kb, turn=1, pending=pending).call("cancel_order", {"order_id": 1002, "confirmed": False})
    out = tools_for(repo, kb, turn=2, message="yes", pending=pending).call("cancel_order", {"order_id": 1002, "confirmed": True})
    assert out["status"] == "cancellation_requested"
    assert repo.get_order("C001", 1002)["shipment"]["cancellation_requested"] == 1


def test_late_third_party_shipment_flags_escalation(repo, kb):
    out = tools_for(repo, kb, "C004").call("track_shipment", {"order_id": 1010})
    assert out["past_grace_period"] and "escalate_to_human" in out["policy"]


def test_escalation_is_idempotent_per_conversation(repo, kb):
    t = tools_for(repo, kb)
    first = t.call("escalate_to_human", {"reason": "customer_request", "priority": "normal", "summary": "wants a human"})
    second = t.call("escalate_to_human", {"reason": "customer_request", "priority": "normal", "summary": "again"})
    assert first["status"] == "ticket_created" and second["status"] == "ticket_already_open"
    assert len(repo.tickets()) == 1


def test_search_registers_citations(repo, kb):
    t = tools_for(repo, kb)
    out = t.call("search_help_center", {"query": "upi refund"})
    assert out["results"][0]["id"] == "KB1" and "KB1" in t.ctx.citations


def test_unknown_tool_and_bad_args_return_errors(repo, kb):
    t = tools_for(repo, kb)
    assert "error" in t.call("delete_database", {})
    assert "error" in t.call("get_order", {"wrong": 1})
