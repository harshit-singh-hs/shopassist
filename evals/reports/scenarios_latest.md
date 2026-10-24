# Scenario eval: pending a complete run

The first full run (2026-10-24) was stopped when the Gemini free tier's **daily** quota ran out (20 requests per model per day; a full run needs about 100). Before that, the first 7 scenarios were graded: 6 passed, and `out_of_stock_replacement_becomes_refund` failed. Its transcript was not saved, because that run predates incremental saving.

The runner now:
- records quota failures as *not graded*
- saves after every scenario
- stops early when the quota is exhausted
- continues with `python -m evals.run_scenarios --resume evals/reports/<run>.jsonl`

This file is overwritten by the next run.
