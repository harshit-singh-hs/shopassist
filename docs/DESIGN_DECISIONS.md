# Design Decisions

Every important choice in ShopAssist is listed here in the same format:
- **Decision**: what I chose.
- **Why**: the reasons.
- **Alternatives**: what else I considered, and why I didn't use it.
- **Trade-off / at scale**: what it costs, and what I would change for a bigger system.

Numbers quoted here come from the eval reports in `evals/reports/`.

---

## A. Problem and data

### A1. A support agent that *takes actions*, not an FAQ bot
- **Decision:** The agent answers questions, but it also tracks orders, checks eligibility, creates return or replacement requests, cancels orders and opens tickets for humans.
- **Why:** Real support work is mostly actions on account data ("where is my order", "return this"). A bot that only answers FAQs deflects a small share of tickets. A bot that acts resolves them. Taking actions also forces the hard problems that interviewers care about: authorization, confirmation, policy enforcement and idempotency.
- **Alternatives:** A pure RAG chatbot is simpler, but it never touches state, so it has none of the safety problems that make this domain interesting.

### A2. Knowledge base = Amazon.in's real public help pages
- **Decision:** `scripts/fetch_kb.py` crawls 43 real help pages and converts them to markdown. It keeps the source URL, breadcrumb, fetch time and content hash for every page.
- **Why:** Real content is messy in a realistic way: long tables, nested lists, boilerplate, navigation blocks, pages that overlap and policies that conflict. A KB written to be easy to retrieve from would hide every problem that the chunking and retrieval have to solve.
- **Alternatives:**
  - *Text I write myself.* Clean, but not credible.
  - *Hugging Face FAQ datasets.* No license or provenance, and often LLM-generated.
  - *The whole help center.* Thousands of device-troubleshooting pages would add noise and lower precision for the intents this agent handles.
- **Trade-off:** The content belongs to Amazon. It is fetched locally for a non-commercial demo, git-ignored, and never redistributed. The UI labels the app as unofficial.

### A3. Curating the crawl, and excluding Amazon Bazaar
- **Decision:** A fixed list of pages is grouped by topic (returns, orders, shipping, payments, Prime, security). Pages from Amazon Bazaar, a separate storefront, are excluded by title.
- **Why:** Bazaar has *different* return and shipping rules. Mixing them in produces contradictory retrievals, such as two different return windows for the same question. This is a data-quality decision, not a model decision.
- **Alternatives:** A breadth-first crawl from the help hub. It's easy, but it pulls in irrelevant and conflicting pages.

### A4. Polite, incremental crawler
- **Decision:** The crawler obeys robots.txt and waits 2 seconds between requests. It caches raw HTML and keeps only `<article class="help-content">`. It compares content hashes with the previous manifest and reports new or changed pages.
- **Why:** Hashes make a refresh *incremental*: only changed pages need re-chunking and re-embedding. A changed page is also the signal to re-run evals, because a policy may have changed.
- **At scale:** Run it as a scheduled job (daily). Have it open a pull request with the diff of changed pages, and re-index only the chunks that changed.

### A5. Synthetic orders, real policy model
- **Decision:** Customers and orders are synthetic, because no company publishes real orders. Everything *about* them follows the real pages:
  - each product carries one of Amazon.in's six **return policy types** ("10 Days Returnable", "7 Days Replacement", "Replacement by Brand", "Service Centre Replacement", "10 Day Free Returns & Exchange", "Non-Returnable")
  - order statuses match the real ones
  - payment methods and shipment types (Amazon-shipped vs third-party) match too
- **Why:** The seed data is designed to hit every policy branch, and dates are relative to today, so scenarios never go stale.

---

## B. Chunking

### B1. Heading-aware chunks with the heading path as a header
- **Decision:** Pages are split on `##` / `###` / `####` headings. Each chunk keeps its path ("Amazon.in Refund Policy > Refund Timelines"), and that header is prepended to the text before embedding and BM25 indexing. The intro text before the first heading becomes an "Overview" chunk. Chunks are at most 1,000 characters.
- **Why:** Help articles are written as self-contained sections, so a section is the natural unit of meaning. The header matters because a table row such as "UPI Linked Bank Account | Up to 5 Working Days" never contains the word "refund". The header supplies it. This is a cheap form of "contextual retrieval".
- **Alternatives:**
  - *Fixed-size windows.* These cut tables and lists in half.
  - *Semantic chunking with embedding breakpoints.* More compute, and it would mostly rediscover the headings that are already there.
  - *Whole pages.* Too long for a 512-token embedding model, and too imprecise.

### B2. Table-aware and list-aware splitting
- **Decision:** An oversized table is split by rows, and **every piece repeats the header row**. Oversized paragraphs are split on list items, then on sentences.
- **Why:** The first version produced a 5,294-character chunk, because a whole numbered list was one "paragraph". The embedding model truncates at 512 tokens, so most of it was invisible. Splitting on list items fixed that (maximum is now 1,139 characters). A table chunk without its header can't be interpreted.

### B3. Cleaning before indexing
- **Decision:**
  - Strip markdown link URLs and keep the anchor text.
  - Drop boilerplate such as "To view in your preferred language…" and "English version will prevail".
  - Drop sections that are navigation only: tables of contents, "Quick Solutions", link lists.
  - Remove `**` markers and re-join hard-wrapped lines.
- **Why:** Some sign-in URLs on these pages are 500+ characters. They waste tokens and dilute both the embeddings and BM25. Boilerplate that repeats on every page makes every page look similar.

---

## C. Retrieval

### C1. Four retrievers behind one interface; the default is chosen by eval
- **Decision:** `KnowledgeBase.search(mode=...)` supports `keyword` (BM25), `dense` (vectors), `hybrid` (BM25 + dense fused with RRF) and `hybrid_rerank` (hybrid plus a cross-encoder). The default is **`dense`**, because that's what the evals chose (see C3).
- **Why build all four:** They fail in different ways:
  - Dense search handles paraphrases ("money got debited but order failed" → "Payment Issues").
  - BM25 handles exact terms ("NEFT", "EMI", "A-to-z", "RRN").

  I expected hybrid to win and built it first. Measuring showed something more nuanced: **hybrid only helps when the embedding model is weak.**
  - With `bge-small`, BM25 fusion raised Hit@1 from 66% to 69%.
  - With the stronger `granite-small-r2`, it *lowered* Hit@1 from 74% to 70%, because BM25 pushes noisy keyword matches above good semantic matches.

  I kept everything configurable (`RETRIEVAL_MODE`), so a bigger KB, with product codes or model numbers for example, can switch back to hybrid if the evals say so.

### C2. Reciprocal Rank Fusion (RRF, k=60)
- **Decision:** The two result lists are merged with `score = Σ 1/(60 + rank)`.
- **Why:** BM25 scores (unbounded) and cosine similarities (−1 to 1) are on different scales. A weighted sum needs normalisation and a tuned weight, and both drift when the corpus changes. RRF uses only ranks, so there is nothing to calibrate. k=60 comes from the original paper (Cormack et al., 2009) and is robust in practice.
- **Alternatives:** Weighted score fusion, which might squeeze out a little more with tuning but is fragile; learned fusion, which needs training data.

### C3. Embedding model and reranker: chosen by measurement, not by habit
- **Method:** `evals/compare_models.py` runs every embedder with dense and hybrid search, then tests every reranker on top of the best embedder. It uses the 70-question golden set and the agent's real k=4, on an idle CPU.

| Embedder | Mode | Hit@1 | Hit@4 | MRR | p50 latency |
|---|---|---|---|---|---|
| **ibm-granite/granite-embedding-small-english-r2** | **dense** | **74%** | 94% | 0.824 | **7 ms** |
| granite-small-r2 | hybrid | 70% | 94% | 0.801 | 8 ms |
| granite-small-r2 | hybrid + MiniLM-L6 reranker | 73% | **97%** | **0.832** | 566 ms |
| granite-small-r2 | hybrid + MiniLM-L12 reranker | 71% | 96% | 0.829 | 882 ms |
| granite-small-r2 | hybrid + jina-turbo reranker | 71% | 94% | 0.814 | 705 ms |
| BAAI/bge-small-en-v1.5 | dense | 66% | 93% | 0.780 | 6 ms |
| bge-small-en-v1.5 | hybrid | 69% | 90% | 0.773 | 6 ms |
| snowflake-arctic-embed-s | hybrid | 54% | 86% | 0.675 | 6 ms |

- **Decision:** Granite small-r2 embeddings, dense retrieval, and no reranker by default.
  - Granite is the best of the models tried on every metric, at the same 384 dimensions and similar speed as bge-small.
  - The best reranker adds +3 points of Hit@4. With 70 questions, that is **2 questions, within noise**. MRR is basically flat (0.824 → 0.832), and it costs about **80× the latency** (7 ms → 566 ms) on CPU.
  - The reranker stays available (`RETRIEVAL_MODE=hybrid_rerank`). I'd revisit it with a GPU or a larger golden set.
- **Honest caveats:**
  - The first version of the golden set had 45 questions. I grew it to 70 *before* switching models, because a 3-question difference isn't a decision.
  - Snowflake's poor score is probably a query-prefix convention it expects, not the model's true quality.
  - Two other candidates (`bge-base-en-v1.5`, `bge-reranker-v2-m3-int8`) failed to download in this environment, so they're untested.
  - Granite ran out of memory at the default embedding batch size (256) because of its long context. Embedding batches of 32 fixed it.

### C4. Local ONNX models (fastembed), not an embeddings API
- **Decision:** The embedding model and the cross-encoder run locally on CPU through ONNX.
- **Why:** Zero cost, no rate limits, works offline, and the documents never leave the machine. Indexing the 307 chunks takes about 90 seconds on CPU, once (then it's cached), and a whole dense search takes about 8 ms.
- **Alternatives:** Gemini or OpenAI embeddings. They're stronger, but they cost money, have rate limits, and add network latency to every query.
- **At scale:** Use a GPU embedding service, or an API with batching. The embedding cache stays the same either way.

### C5. Qdrant as the vector store
- **Decision:** Qdrant runs in-process for development (`location=":memory:"`). Setting `QDRANT_URL` switches to a Qdrant server; the code path is the same.
- **Why:** Payload filtering (by topic, for example), HNSW indexing, and a clean path from a laptop to a production server.
- **Alternatives:**
  - *FAISS.* A fast library, but you build persistence and filtering yourself.
  - *Chroma.* Fine, but has heavier dependencies.
  - *pgvector.* My choice if orders were already in Postgres, because it's one database to operate. I'd mention that trade-off honestly.
  - *A numpy matrix.* Honestly enough for 300 chunks, but it doesn't scale and doesn't show the production pattern.

### C6. Embedding cache keyed by content hash
- **Decision:** Vectors are stored in `index/kb_embeddings_<model>.npz`, keyed by a SHA-1 hash of each chunk's content.
- **Why:** A cold start takes 50–90 seconds to embed, depending on the model. A warm start embeds 0 chunks. After a KB refresh, only changed chunks are re-embedded. The model name is part of the file name, so switching models can never mix vectors from two models.

### C7. Top-k = 4 passages
- **Why:** The 4 passages are about 2,500 characters, which is enough context for the multi-part answers support needs, without burying the right passage. Hit@4 is 94%. The agent can also search again with a different query if the first results are weak.

---

## D. LLM and agent

### D1. Gemini 3.5 Flash, pinned, with a fallback chain
- **Decision:** The primary model is `gemini-3.5-flash`, with fallbacks `gemini-3-flash-preview` → `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemini-2.5-flash`. Versions are pinned, and `-latest` aliases are deliberately avoided.
- **Why:**
  - It is free-tier, fast, and has strong native function calling.
  - Pinning matters: an alias can switch the underlying model overnight and silently change behaviour. Upgrades should go through evals.
  - I found this the practical way: `gemini-2.5-flash-lite` (my first choice for the judge) returned **404, model retired**, during development.
- **Alternatives:**
  - *GPT-4o-mini or Claude.* Comparable or better quality, but paid.
  - *Llama or Qwen locally via Ollama.* Free, but tool calling is weaker and there's no GPU here.
  - The provider lives behind one module (`llm.py`), so switching is contained.

### D2. Quota-aware routing, retries and backoff
- **Decision:** Each model gets its own sliding-window rate budget (`MAX_RPM`). A request goes to the first model in the chain that has capacity. A model that returns 429 cools down for 60 seconds. 5xx errors are retried with exponential backoff plus jitter. 4xx errors fail fast, because they're our bug and another model won't fix them.
- **Why, in three iterations:**
  1. A *single global* limiter made one turn take **53 seconds**: it waited on a model that was out of quota while another model was idle. Free-tier quotas are per model, so the router has to be per model too.
  2. A fixed 60-second cooldown after a 429 broke once the eval hit the **per-day** quota (20 requests per model per day). The router kept retrying exhausted models every minute.
  3. Now it parses the server's `retryDelay` and quota type from the 429, and benches the model for exactly that long (seconds or hours). A chat request waits at most `LLM_MAX_WAIT_S` (20 s) and then fails fast with a graceful reply. The chain has 5 models, all verified to support tool calling, which is about 100 free requests a day. The dashboard and `/health` show each model's quota status.
- **Eval hygiene:** a scenario where the LLM was unavailable is recorded as *not graded* (infrastructure), never as an agent failure. Runs are saved after every scenario and continue the next day with `--resume`.

### D3. Native function calling in a hand-written loop (no LangChain / LangGraph)
- **Decision:** `agent.py` is a loop of about 150 lines: call the model, execute any tool calls, feed the results back, repeat until the model returns text. There's a step budget of 8.
- **Why:**
  - I control and can explain every step: retries, tool errors, guardrail hooks, tracing, and the self-correction retry.
  - Debugging means reading my own code, not framework internals.
  - Gemini 3 needs its "thought signatures" passed back exactly. Owning the loop means that is handled correctly and visibly.
- **Alternatives:**
  - *LangGraph.* A good choice for long-running workflows that need durable checkpoints, human-in-the-loop pauses, or branching graphs. I'd adopt it if conversations had to survive restarts mid-action.
  - *LangChain agents / CrewAI.* More abstraction than this needs.

### D4. One agent, not multi-agent
- **Why:** The domain is narrow: 8 tools that all fit in one context. A router plus specialist agents adds LLM calls (latency and cost), plus hand-off failures, where context is lost between agents. Multi-agent pays off when tool sets are large and disjoint, or the tasks are very different.

### D5. Thinking level "low", default temperature
- **Decision:** Gemini 3's thinking is set to `low`, and the temperature is left at the model default.
- **Why:**
  - Measured on a single tool-calling request: `minimal` took 1.8 s, the default took 3.5 s, and `low` took 5.6 s. With samples this small, variance dominates; the scenario evals decide.
  - Google recommends keeping Gemini 3 at its default temperature.
  - Determinism here doesn't come from temperature. It comes from policy code, the confirmation gates, and evals.

### D6. Pre-fetching recent orders into the system prompt
- **Why:** Most conversations are about a recent order ("my watch", "my last order"). Putting a one-line summary of the last 8 orders in the prompt saves a `list_orders` round trip (1–2 s) in most conversations, for about 150 extra tokens per call. The tools still fetch live details before acting.

### D7. Memory: a sliding window
- **Decision:** The last 10 turns are kept verbatim, including tool calls and results. Older turns keep only the user message and the final reply.
- **Why:** Support chats are short and recent turns matter most. This bounds tokens without forgetting the thread.
- **Alternatives:**
  - *Rolling LLM summaries.* An extra call per turn.
  - *Vector memory.* Overkill: you don't need semantic search over a 6-message chat.

### D8. The prompt is versioned and evaluated
- **Decision:** One system prompt with `PROMPT_VERSION`. Every trace records the prompt version and the model.
- **Why:** A prompt change is a behaviour change, so it gets the same treatment as a code change: edit, then run evals, then compare.

---

## E. Safety and trust (the core of the design)

### E1. Policies live in code: "the LLM proposes, the code decides"
- **Decision:** `policies.py` encodes the real rules as pure functions:
  - the six return policy types
  - the 5-day window for reporting damaged non-returnable items
  - "out of stock → refund instead of replacement"
  - cancellation before vs after shipping
  - delay grace periods (1 day for Amazon-shipped, 5 days for third-party couriers)
- **Why:** A model can be persuaded; an `if` statement can't. Even a fully jailbroken model can only *call tools*, and the tools refuse out-of-policy actions.
- **Alternatives:** Writing the policy into the prompt. It works most of the time, which is not good enough for money.

### E2. Identity comes from the session, never from the chat
- **Decision:** Every repository query filters on `customer_id` *in SQL*, and that ID comes from the login session. Tools take no customer argument.
- **Why:** "Show me order 1005" from another customer returns the same "not found" whether the order exists or not. Data from another account is unreachable, and the bot can't be used to check which order numbers exist.

### E3. Two-phase confirmation, enforced in code
- **Decision:** A write tool called with `confirmed=false` registers a pending action and returns a summary. With `confirmed=true`, it executes only if (a) the proposal was made in an **earlier turn** and (b) the customer's latest message is affirmative.
- **Why:** "Ask before acting" written in a prompt is a suggestion. Here it's a gate. The UI also renders Confirm / Don't buttons.
- **Found in testing:** the model sometimes asked "shall I proceed?" *without* registering a proposal. The gate then correctly refused to execute, and the customer was asked twice. The fix was in the tool description and prompt ("call with confirmed=false in the same turn"), and the eval scenario covers it.

### E4. Human-in-the-loop for high-value refunds
- **Decision:** Refunds above ₹10,000 are created as `pending_approval`. They appear in the dashboard's approval queue for a human to approve or reject.
- **Why:** This is an internal risk control. Real retailers review high-value or unusual refunds for fraud. The threshold is configurable.

### E5. Deterministic guardrails, not LLM guard models
- **Decision:** The guardrails are regex and rule-based, and every one is unit-tested:
  - payment-card numbers are redacted (Luhn-checked) and CVV/OTP/PIN codes are removed *before* the LLM sees the text
  - emails and phone numbers are masked before anything is stored
  - prompt-injection patterns are flagged
  - legal or safety keywords route the conversation to an urgent escalation
- **Why:** About 0 ms of latency, no API cost, predictable behaviour, and easy to test. Luhn validation stops order IDs, phone numbers and tracking codes from being redacted by mistake.
- **Alternatives:** Llama Guard, NeMo Guardrails or a moderation API. They're better at fuzzy cases (toxicity, subtle jailbreaks), but each adds a model call to every turn. I'd add one as an *async* second layer at scale.

### E6. Prompt injection: defence in depth
- **Layers:**
  1. Detection flags the message and adds a system note.
  2. The prompt declares that tool results and help-center text are data, not instructions.
  3. **Structural limits**: customer-scoped tools, policy in code, confirmation gates.
- **Why:** Layer 3 is what makes injection *harmless* rather than merely unlikely. A detector alone always misses something, and blocking every flagged message would hurt real customers ("ignore my previous message" is a normal thing to say, and the tests cover it).

### E7. Output checks and one self-correction retry
- **Checks:**
  - **Unperformed-action claims:** "I've cancelled your order" with no successful action tool. One corrective retry is triggered.
  - **Possible data leaks:** order IDs that are neither the customer's own nor ones they typed. The reply is blocked.
  - **Citation validity:** every [KB#] must point to a passage that was actually retrieved.
- **Why:** Claiming an action that never happened is the most damaging failure mode for an agent in production. Catching it with a deterministic check is cheap.

### E8. Idempotency and re-checking at execution time
- **Decision:** `UNIQUE(order_item_id)` on return requests makes retries safe. Eligibility is re-checked when the action *executes*, not only when it's proposed. Cancelling uses a conditional `UPDATE … WHERE status IN (…)`.
- **Why:** Retries happen (network, model retries, double clicks). State can change between the proposal turn and the confirmation turn (stock, status). This prevents "time-of-check vs time-of-use" bugs.

---

## F. Evaluation

### F1. Two levels of evals
- **Retrieval eval** (`evals/run_retrieval.py`): 45 customer-style questions, each labeled with the real page(s) that answer it. Hit@1, Hit@4, MRR and latency for every retriever. It runs locally, is free, and takes seconds, so it runs on every chunking or model change.
- **Scenario eval** (`evals/run_scenarios.py`): 27 scripted conversations covering returns, cancellations, tracking, policy Q&A, escalation and red-team attacks. They run against the real LLM, real tools and a fresh database each time.

### F2. Grade on database state, not on wording
- **Why:** "Did a return request for order 1001 with resolution=refund actually get created?" is objective and unambiguous. Regex checks on wording cover the rest, including **forbidden** patterns (leaking another customer's address, claiming a refund was issued, leaking the system prompt).
- **LLM-as-judge** (`--judge`) is a *secondary* signal for tone, helpfulness and groundedness. The judge is a different, cheaper model, which reduces self-preference bias. It is never the pass/fail gate, because judges are noisy and biased toward long answers.

### F3. pass^k for reliability
- **Decision:** `--repeat k` re-runs each scenario. A scenario counts only if it passes **every** run (pass^k, as popularised by τ-bench).
- **Why:** LLM agents are non-deterministic. pass@1 tells you it *can* work; pass^k tells you it works *reliably*, which is what production needs.

### F4. Unit tests with a scripted fake LLM
- **Decision:** 90 tests run in about 6 seconds with no API key. A fake `generate()` returns scripted function calls and text, so the agent loop, guardrails, the step budget and the outage fallback are all tested deterministically.
- **KB-consistency tests:** these check that the crawled pages still contain the exact facts the policy engine encodes ("within 5 days from the delivery", "Up to 5 Working Days", …). If Amazon changes a policy page, CI fails instead of the bot silently giving outdated answers.

---

## G. Engineering

### G1. SQLite (WAL mode) behind a repository layer
- **Why:** No setup, a single file, and WAL mode lets the dashboard read while the agent writes. All SQL is parameterised, so tool arguments can't inject SQL. The schema is plain SQL that ports to Postgres.
- **At scale:** Postgres, with connection pooling. Sessions would move to Redis so any API instance can serve any conversation.

### G2. One service layer, one FastAPI process, a hand-built UI
- **Decision:** `SupportService` is the single entry point (sessions, persistence, admin operations). FastAPI exposes it as a REST API and also serves the web UI from `web/`: plain HTML, CSS and an ES module, with no framework and no build step.
- **Why:**
  - One container is the cheapest thing to deploy for free.
  - The UI is a client of the same public API a website widget would use, so there's no hidden shortcut around the auth rules.
  - I first used Streamlit. It was quick, but it looks like every Streamlit app, and the UI is part of how the project is judged.
- **Alternatives:** React or Next.js would add a build pipeline and a second deployable, which is worth it for a big app, not for four views.
- **Design:** "Paper and ink": warm neutrals, one terracotta accent, a serif display face, and monospace for data. It deliberately avoids the dark-blue, purple-gradient look of generated UIs. There's a warm dark mode, and a layout that works on phones.
- **One account on screen:** the chat shows only the signed-in customer's account and orders, as a real store would. Switching between the six demo customers is a clearly labelled *Demo* control (with what each one is good for testing), so the demo never looks like several people are logged in.
- **Transparency is a feature:** every reply can be expanded in an *Under the hood* panel (model calls, tool calls with arguments and results, guardrail flags, and the cited pages). It doubles as a debugging tool and as the best way to demo the design.

### G3. Observability built in
- **Decision:** Every turn writes a trace row: latency, LLM calls, tool calls and errors, tokens, guardrail flags, model and prompt version, plus a per-step event list.
- **Dashboard KPIs:**
  - containment rate (resolved without a human)
  - escalations
  - pending approvals
  - average and p95 latency
  - estimated cost at list prices
  - tool error rate
  - flagged turns
- **At scale:** Export to OpenTelemetry or Langfuse, and alert on p95 latency, error rate and drops in containment.

### G4. Two benchmarking lessons I learned the hard way
1. **A misleading micro-benchmark.**
   - What I did: I timed the reranker on the *same* 10 short passages, repeated, and saw 917 ms → 149 ms from setting `batch_size`. I claimed "6× faster".
   - What was true: on realistic queries, each with different passages, it was 482 ms → 391 ms. About 20%.
   - Lesson: benchmark with realistic, varied inputs.
2. **Contention inflates latency.**
   - What I did: the first retrieval eval reported 81 ms for dense search and 2.4 s for reranking, while another benchmark was running in the background.
   - What was true: on an idle machine, the same configurations took 8 ms and 555 ms.
   - Lesson: every latency number in these docs was measured with nothing else running.

---

### G5. Free deployment, with abuse protection
- **Decision:** Render's free web service, from GitHub, using a `render.yaml` blueprint: the Docker image, the free plan, a health check, and secrets entered in Render, never committed.
- **How I got there (measure, don't guess):**
  - I first planned Hugging Face Spaces, because the docs listed a free 16 GB CPU tier. Creating the Space returned **402 Payment Required**: Docker Spaces now need PRO.
  - I had also ruled out Render, guessing the app needed about 1 GB. I then **measured** the running server at about **350 MB**, which fits Render's 512 MB.
  - The Hugging Face script (`scripts/deploy_hf.py`) remains for anyone with PRO.
- **Free-tier trade-offs, said openly:**
  - The free instance sleeps after 15 idle minutes, and waking takes 1–2 minutes on a slow shared CPU, so I open the link before an interview.
  - Demo data resets on sleep. That's acceptable for a demo; in production, data would live in Postgres.
- **Why not fully serverless (e.g. Vercel functions):** size would fit (~400 MB against a 500 MB Python limit), but state wouldn't. The two-step confirmation and the conversation memory live in the session, and the demo data lives in SQLite. Serverless instances are short-lived and requests can land on different ones, so a "yes" could reach an instance that never saw the proposal. The supported split is: the static UI on Vercel (`vercel.json`, which builds `config.js` from `SHOPASSIST_API`), and the API on Render with CORS limited to that origin. Going fully serverless would mean moving sessions and data into Postgres or Redis.
- **Protection for a public demo on a free LLM key:**
  - an optional access code
  - a per-IP rate limit
  - per-conversation and daily turn caps
  - demo data re-seeded daily, because its dates are relative and would otherwise drift
- **The build fails loudly:** the crawler retries rate-limited pages, keeps the previous copy on a transient failure, and exits with an error if a core policy page is missing. A blocked build server stops the deploy instead of shipping a bot without its returns policy.
  - Found in testing: a fresh crawl got a transient HTTP 403 on exactly the returns-policy page, and the old crawler silently skipped it.

## H. Known limitations (said honestly in interviews)
- Orders are synthetic. The policies are real, but real order data has edge cases this doesn't (split shipments, partial refunds, seller-fulfilled items).
- English only. Hinglish ("mera order kab aayega?") would need multilingual embeddings (e.g. `paraphrase-multilingual-MiniLM`) and eval questions in that style.
- The confirmation check is English-keyword based. In production the UI's Confirm button would send a structured confirmation instead of free text.
- The golden set has 70 questions, so each one is about 1.4 percentage points. Differences of 2–3 points between retrievers are within noise; I'd grow it from real support logs.
- No streaming responses yet (the turn appears all at once). That would be the next UX improvement.
