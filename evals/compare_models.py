"""Model selection for retrieval: every embedder x (no reranker | each reranker) on the labeled set.
This is how the defaults in config.py were chosen; re-run it when the KB or the question set changes.

Usage:  python -m evals.compare_models
"""
import time
from pathlib import Path

import yaml

from shopassist.config import settings
from shopassist.retrieval import KnowledgeBase, load_knowledge_base

from .run_retrieval import evaluate

HERE = Path(__file__).parent
# bge-base-en-v1.5 and bge-reranker-v2-m3-int8 were also tried but failed to download from the model hub here.
EMBEDDERS = ["BAAI/bge-small-en-v1.5", "snowflake/snowflake-arctic-embed-s", "ibm-granite/granite-embedding-small-english-r2"]
RERANKERS = ["Xenova/ms-marco-MiniLM-L-6-v2", "Xenova/ms-marco-MiniLM-L-12-v2", "jinaai/jina-reranker-v1-turbo-en"]


def main():
    questions = yaml.safe_load((HERE / "retrieval_questions.yaml").read_text(encoding="utf-8"))
    chunks = load_knowledge_base()
    k = settings.top_k
    rows, kbs, failures = [], {}, []
    # Stage 1: every embedder, dense and hybrid (cheap).
    for emb in EMBEDDERS:
        t0 = time.perf_counter()
        try:
            kbs[emb] = KnowledgeBase(chunks, embed_model=emb)
        except Exception as e:  # e.g. ONNX out-of-memory on long-context models; record and move on
            print(f"{emb:<48} FAILED to index: {type(e).__name__}: {str(e)[:120]}", flush=True)
            failures.append((emb, f"{type(e).__name__}: {str(e)[:120]}"))
            continue
        build_s = time.perf_counter() - t0
        for mode in ("dense", "hybrid"):
            r = evaluate(kbs[emb], questions, mode, k)
            rows.append((emb, mode, "-", r))
            print(f"{emb:<48} {mode:<14} {'-':<36} hit@1 {r['hit@1']:.0%} hit@{k} {r[f'hit@{k}']:.0%} "
                  f"mrr {r['mrr']:.3f} p50 {r['p50_ms']:.0f}ms (index {build_s:.0f}s)", flush=True)
    # Stage 2: rerankers only on top of the best embedder (rerankers are the expensive part).
    best = max(rows, key=lambda row: row[3]["mrr"])[0]  # embedder of the best first-stage config
    kb = kbs[best]
    for rr in RERANKERS:
        kb.rerank_model = rr
        kb.__dict__.pop("reranker", None)  # reset cached_property
        kb.search("warm up", mode="hybrid_rerank")
        r = evaluate(kb, questions, "hybrid_rerank", k)
        rows.append((best, "hybrid_rerank", rr, r))
        print(f"{best:<48} {'hybrid_rerank':<14} {rr:<36} hit@1 {r['hit@1']:.0%} hit@{k} {r[f'hit@{k}']:.0%} "
              f"mrr {r['mrr']:.3f} p50 {r['p50_ms']:.0f}ms", flush=True)

    lines = [f"Model comparison: {len(questions)} questions, {len(chunks)} chunks, k={k}", "",
             f"| Embedder | Mode | Reranker | Hit@1 | Hit@{k} | MRR | p50 latency |", "|---|---|---|---|---|---|---|"]
    for emb, mode, rr, r in sorted(rows, key=lambda x: -x[3]["mrr"]):
        lines.append(f"| {emb} | {mode} | {rr} | {r['hit@1']:.0%} | {r[f'hit@{k}']:.0%} | {r['mrr']:.3f} | {r['p50_ms']:.0f} ms |")
    lines += [""] + [f"Failed to run: {emb} ({err})" for emb, err in failures] if failures else []
    (HERE / "reports").mkdir(exist_ok=True)
    (HERE / "reports" / "model_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
