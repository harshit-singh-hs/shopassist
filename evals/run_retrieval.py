"""Retrieval eval: compares keyword (BM25), dense, hybrid (RRF) and hybrid + cross-encoder rerank on the
labeled question set. Runs fully locally (no API calls), so it's cheap enough for every change.

Metrics (page level: a hit is any retrieved chunk from an expected page):
  Hit@1  - the top result is from a correct page
  Hit@k  - a correct page is in the top k (k = what the agent actually sees)
  MRR    - mean of 1/rank of the first correct result
  p50 ms - median latency per query

Usage:  python -m evals.run_retrieval
"""
import statistics
import time
from pathlib import Path

import yaml

from shopassist.config import settings
from shopassist.retrieval import MODES, KnowledgeBase

HERE = Path(__file__).parent


def evaluate(kb: KnowledgeBase, questions: list[dict], mode: str, k: int) -> dict:
    hit1 = hitk = rr = 0.0
    latencies, misses = [], []
    for item in questions:
        t0 = time.perf_counter()
        hits = kb.search(item["q"], k=k, mode=mode)
        latencies.append((time.perf_counter() - t0) * 1000)
        ranks = [i for i, h in enumerate(hits, 1) if h["doc_id"] in item["pages"]]
        if ranks:
            hit1 += ranks[0] == 1
            hitk += 1
            rr += 1 / ranks[0]
        else:
            misses.append((item["q"], hits[0]["doc_id"] if hits else None))
    n = len(questions)
    return {"mode": mode, "hit@1": hit1 / n, f"hit@{k}": hitk / n, "mrr": rr / n,
            "p50_ms": statistics.median(latencies), "misses": misses}


def main():
    questions = yaml.safe_load((HERE / "retrieval_questions.yaml").read_text(encoding="utf-8"))
    kb = KnowledgeBase()
    known = {c["doc_id"] for c in kb.chunks}
    unknown = {p for q in questions for p in q["pages"]} - known
    assert not unknown, f"labels reference pages not in the KB: {unknown}"
    kb.search("warm up", mode="hybrid_rerank")  # load the reranker before timing

    k = settings.top_k
    results = [evaluate(kb, questions, mode, k) for mode in MODES]
    lines = [f"Retrieval eval: {len(questions)} questions, {len(kb.chunks)} chunks from {len(known)} real pages, k={k}", "",
             f"| Retriever | Hit@1 | Hit@{k} | MRR | p50 latency |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['mode']} | {r['hit@1']:.0%} | {r[f'hit@{k}']:.0%} | {r['mrr']:.3f} | {r['p50_ms']:.0f} ms |")
    best = next(r for r in results if r["mode"] == settings.retrieval_mode)  # misses of the configured default
    if best["misses"]:
        lines += ["", f"Misses for {best['mode']}:"] + [f"- {q}  (top result: {top})" for q, top in best["misses"]]
    report = "\n".join(lines)
    print(report)
    (HERE / "reports").mkdir(exist_ok=True)
    (HERE / "reports" / "retrieval_latest.md").write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
