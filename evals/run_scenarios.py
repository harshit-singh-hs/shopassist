"""End-to-end scenario eval: real LLM + real tools + real knowledge base, a fresh seeded DB per scenario,
graded by deterministic checks (mostly on database state). Optional LLM-as-judge for answer quality.

Usage:
  python -m evals.run_scenarios                      # all scenarios once
  python -m evals.run_scenarios --category security  # one category
  python -m evals.run_scenarios --only window_expired,card_number_redacted
  python -m evals.run_scenarios --repeat 3           # reliability: pass^3 = passes all 3 runs
  python -m evals.run_scenarios --judge              # + LLM-as-judge quality scores (extra API calls)
"""
import argparse
import json
import math
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from google.genai import types

from shopassist import llm
from shopassist.config import settings
from shopassist.db.repository import Repository
from shopassist.db.seed import seed
from shopassist.prompts import PROMPT_VERSION
from shopassist.retrieval import KnowledgeBase
from shopassist.service import SupportService

HERE = Path(__file__).parent

JUDGE_PROMPT = """You are grading a customer-support AI agent. Score the agent's replies from 1 (bad) to 5 (excellent):
- helpfulness: did it resolve the request or give a clear next step?
- groundedness: are its factual claims supported by the tool results / help-center passages shown? (5 = nothing invented)
- tone: warm, concise, professional?
Return JSON: {"helpfulness": int, "groundedness": int, "tone": int, "comment": "one sentence"}

CONVERSATION:
{conversation}

TOOL RESULTS AND PASSAGES THE AGENT SAW:
{evidence}"""


def _match(row: dict, want: dict) -> bool:
    return all(row.get(k) == v for k, v in want.items())


def check(sc: dict, results: list, repo: Repository) -> list[str]:
    exp, cid, fails = sc["expect"], sc["customer"], []
    tools = [e["name"] for r in results for e in r.trace["events"] if e["kind"] == "tool"]
    fails += [f"tool {t} not called" for t in exp.get("tools_called", []) if t not in tools]
    fails += [f"tool {t} called" for t in exp.get("tools_not_called", []) if t in tools]

    final, everything = results[-1].reply, "\n".join(r.reply for r in results)
    if exp.get("reply_any") and not any(re.search(p, final, re.I) for p in exp["reply_any"]):
        fails.append(f"final reply matched none of {exp['reply_any']}")
    fails += [f"reply matched forbidden /{p}/" for p in exp.get("reply_none", []) if re.search(p, everything, re.I)]
    flags = {f for r in results for f in r.flags}
    fails += [f"flag {f} not raised" for f in exp.get("flags_include", []) if f not in flags]
    if exp.get("must_cite") and not results[-1].citations:
        fails.append("final reply has no valid [KB#] citation")

    db = exp.get("db", {})
    if "return_request" in db:
        reqs, want = repo.returns_for_customer(cid), db["return_request"]
        if want == "none" and reqs:
            fails.append(f"unexpected return request(s): {[(r['order_id'], r['resolution'], r['status']) for r in reqs]}")
        elif want != "none" and not any(_match(r, want) for r in reqs):
            fails.append(f"no return request matching {want}; have {[(r['order_id'], r['resolution'], r['status']) for r in reqs]}")
    if "order_status" in db:
        order = repo.get_order(cid, db["order_status"]["order_id"])
        if order["status"] != db["order_status"]["status"]:
            fails.append(f"order {order['id']} status is {order['status']}, expected {db['order_status']['status']}")
    if "cancellation_requested" in db:
        orders = [repo.get_order(cid, o["id"]) for o in repo.list_orders(cid, limit=100)]
        flagged = [o["id"] for o in orders if o["shipment"] and o["shipment"]["cancellation_requested"]]
        want = db["cancellation_requested"]
        if (want == "none" and flagged) or (want != "none" and want not in flagged):
            fails.append(f"cancellation_requested expected {want}, got {flagged}")
    if "ticket" in db:
        tickets, want = [t for t in repo.tickets(None) if t["customer_id"] == cid], db["ticket"]
        if want == "none" and tickets:
            fails.append("unexpected ticket")
        elif want != "none" and not any(_match(t, want or {}) for t in tickets):
            fails.append(f"no ticket matching {want}; have {[t['priority'] for t in tickets]}")
    return fails


def judge(sc: dict, results: list) -> dict:
    conversation = "\n".join(f"CUSTOMER: {t}\nAGENT: {r.reply}" for t, r in zip(sc["turns"], results))
    evidence = "\n".join(e.get("result", "")[:600] for r in results for e in r.trace["events"] if e["kind"] == "tool")
    prompt = JUDGE_PROMPT.replace("{conversation}", conversation).replace("{evidence}", evidence or "(none)")
    try:
        resp = llm.generate(prompt, types.GenerateContentConfig(response_mime_type="application/json"),
                            model=settings.judge_model)
        return json.loads(resp.text)
    except Exception as e:  # judging is best-effort; a judge failure must not fail the run
        return {"error": str(e)[:200]}


def run_one(sc: dict, kb: KnowledgeBase, with_judge: bool) -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = Repository(seed(Path(tmp) / "eval.db"))
        svc = SupportService(repo=repo, kb=kb)
        session = svc.start_conversation(sc["customer"])
        results = [svc.send(session.id, turn) for turn in sc["turns"]]
        fails = check(sc, results, repo)
    # An unavailable LLM (quota exhausted, outage) says nothing about the agent: it is an infrastructure
    # ERROR, reported separately and excluded from the pass rate, never counted as a FAIL.
    infra_error = any("llm_error" in r.flags for r in results)
    status = "ERROR" if infra_error else ("PASS" if not fails else "FAIL")
    record = {
        "id": sc["id"], "category": sc["category"], "status": status, "passed": status == "PASS",
        "failures": ["LLM unavailable (quota/outage)"] if infra_error else fails,
        "turns": [{"user": t, "reply": r.reply, "flags": r.flags, "latency_ms": r.trace["latency_ms"],
                   "llm_calls": r.trace["llm_calls"], "input_tokens": r.trace["input_tokens"],
                   "output_tokens": r.trace["output_tokens"], "model": r.trace["model"],
                   "tools": [e["name"] for e in r.trace["events"] if e["kind"] == "tool"]}
                  for t, r in zip(sc["turns"], results)],
    }
    if with_judge and not infra_error:
        record["judge"] = judge(sc, results)
    return record


def summarize(records: list[dict], repeat: int) -> str:
    graded = [r for r in records if r["status"] != "ERROR"]
    errored = sorted({r["id"] for r in records if r["status"] == "ERROR"} - {r["id"] for r in graded})
    lines = [f"# Scenario eval ({datetime.now():%Y-%m-%d %H:%M})", ""]
    if not graded:
        return "\n".join(lines + [f"No scenario could be graded: LLM unavailable for {len(errored)} scenario(s)."])
    by_id = defaultdict(list)
    for r in graded:
        by_id[r["id"]].append(r)
    turns = [t for r in graded for t in r["turns"]]
    lat = sorted(t["latency_ms"] for t in turns)
    cost = sum(t["input_tokens"] * settings.price_in_per_m + t["output_tokens"] * settings.price_out_per_m
               for t in turns) / 1e6
    cats = defaultdict(lambda: [0, 0])
    for runs in by_id.values():
        cats[runs[0]["category"]][0] += all(r["passed"] for r in runs)
        cats[runs[0]["category"]][1] += 1
    passed_all = sum(all(r["passed"] for r in runs) for runs in by_id.values())
    models = Counter(m for t in turns for m in t["model"].split(","))

    lines += [f"Primary model: {settings.llm_model} (fallbacks: {', '.join(settings.fallback_models)}), "
              f"thinking={settings.thinking_level}, prompt {PROMPT_VERSION}. LLM calls served by: {dict(models)}", "",
              f"- **Scenarios passed: {passed_all}/{len(by_id)} graded**"
              + (f" (pass^{repeat}: a scenario counts only if it passed all {repeat} runs)" if repeat > 1 else "")]
    if repeat > 1:
        lines.append(f"- Run-level pass rate: {sum(r['passed'] for r in graded) / len(graded):.0%} over {len(graded)} runs")
    if errored:
        lines.append(f"- Not graded (LLM quota/outage: infrastructure, not the agent): {len(errored)}: {', '.join(errored)}")
    lines += [f"- Latency per turn: p50 {lat[len(lat) // 2] / 1000:.1f}s, p95 {lat[math.ceil(0.95 * len(lat)) - 1] / 1000:.1f}s "
              "(free tier, includes rate-limit waits)",
              f"- LLM calls per turn: {statistics.mean(t['llm_calls'] for t in turns):.1f}; "
              f"tokens per conversation: {sum(t['input_tokens'] + t['output_tokens'] for t in turns) / len(graded):,.0f}",
              f"- Estimated cost at paid list prices: ${cost / len(graded):.4f} per conversation (free tier: $0)", "",
              "| Category | Passed |", "|---|---|"]
    lines += [f"| {c} | {p}/{n} |" for c, (p, n) in sorted(cats.items())]
    judged = [r["judge"] for r in graded if "judge" in r and "error" not in r["judge"]]
    if judged:
        lines += ["", "LLM-as-judge (1-5): " + ", ".join(
            f"{k} {statistics.mean(j[k] for j in judged):.2f}" for k in ("helpfulness", "groundedness", "tone"))]
    lines += ["", "| Scenario | Result | Notes |", "|---|---|---|"]
    for sid, runs in by_id.items():
        ok = sum(r["passed"] for r in runs)
        notes = "; ".join(dict.fromkeys(f for r in runs for f in r["failures"]))[:300]
        lines.append(f"| {sid} | {'PASS' if ok == len(runs) else 'FAIL'} ({ok}/{len(runs)}) | {notes} |")
    lines += [f"| {sid} | NOT GRADED | LLM unavailable |" for sid in errored]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--category")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--model", help="pin one model for the whole run (no fallback), for clean comparisons")
    ap.add_argument("--resume", help="path to a previous .jsonl run: skip the scenarios it already graded")
    ap.add_argument("--max-errors", type=int, default=2, help="stop after N consecutive LLM-unavailable scenarios")
    args = ap.parse_args()

    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    if args.category:
        scenarios = [s for s in scenarios if s["category"] == args.category]
    if args.model:
        llm.chain = lambda: [args.model]
    llm.set_max_wait(75)  # a batch job can wait out per-minute limits; interactive chat fails fast instead

    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    run_file = Path(args.resume) if args.resume else out / f"scenarios_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    previous = [json.loads(line) for line in run_file.read_text(encoding="utf-8").splitlines()] if run_file.exists() else []
    records = [r for r in previous if r["status"] != "ERROR"]
    done = {r["id"] for r in records}

    kb = KnowledgeBase()
    consecutive_errors, stopped = 0, False
    for _ in range(args.repeat):
        for sc in scenarios:
            if args.repeat == 1 and sc["id"] in done:
                continue
            rec = run_one(sc, kb, args.judge)
            records.append(rec)
            with open(run_file, "a", encoding="utf-8") as f:  # incremental: a partial run is never lost
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{rec['status']}] {sc['id']:<45} {sum(x['latency_ms'] for x in rec['turns']) / 1000:5.1f}s "
                  f"{rec['turns'][-1]['model']:<24} {'; '.join(rec['failures'])[:150]}", flush=True)
            consecutive_errors = consecutive_errors + 1 if rec["status"] == "ERROR" else 0
            if consecutive_errors >= args.max_errors:
                print(f"\nStopping: LLM unavailable for {consecutive_errors} scenarios in a row. {llm.quota_status()}\n"
                      f"Resume after the quota resets:  python -m evals.run_scenarios --resume {run_file}", flush=True)
                stopped = True
                break
        if stopped:
            break

    report = summarize(records, args.repeat)
    if stopped:
        report += f"\n\n_Partial run (stopped on LLM quota). Resume with `--resume {run_file.as_posix()}`._"
    (out / "scenarios_latest.md").write_text(report + "\n", encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    main()
