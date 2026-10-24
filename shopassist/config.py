"""Central settings. Every value can be overridden with an environment variable or a .env entry."""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # --- LLM (Gemini free tier). Versions are pinned: "-latest" aliases change behaviour silently. ---
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-3.5-flash")
    # Tried in order when the primary is overloaded (503) or out of quota (429).
    # Each model has its own free-tier quota (20 requests/day each at the time of writing), so a longer chain
    # means more daily capacity. All of these were verified to support function calling.
    fallback_models: tuple = tuple(m for m in os.getenv(
        "FALLBACK_MODELS", "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash").split(",") if m)
    llm_max_wait_s: float = _float("LLM_MAX_WAIT_S", "20")  # longest a request waits for a free model
    judge_model: str = os.getenv("JUDGE_MODEL", "gemini-3.1-flash-lite")
    thinking_level: str | None = os.getenv("THINKING_LEVEL", "low") or None  # minimal | low | medium | high
    # Unset = model default. Gemini 3 is tuned for its default temperature; determinism here comes from
    # policy code + evals, not from sampling temperature.
    temperature: float | None = float(os.environ["LLM_TEMPERATURE"]) if os.getenv("LLM_TEMPERATURE") else None
    max_rpm: int = int(os.getenv("MAX_RPM", "5"))  # per-model client-side budget, below free-tier limits
    # Reference list prices (USD per 1M tokens) used only to *estimate* cost in the dashboard.
    price_in_per_m: float = _float("PRICE_INPUT_PER_M", "0.30")
    price_out_per_m: float = _float("PRICE_OUTPUT_PER_M", "2.50")

    # --- Local models (ONNX via fastembed: free, offline, no rate limits) ---
    # Chosen by evals/compare_models.py (70-question golden set): best Hit@1 / Hit@4 / MRR of the models tried.
    embed_model: str = os.getenv("EMBED_MODEL", "ibm-granite/granite-embedding-small-english-r2")
    rerank_model: str = os.getenv("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")

    # --- Retrieval ---
    qdrant_url: str | None = os.getenv("QDRANT_URL") or None  # unset -> in-process Qdrant
    kb_collection: str = "help_center"
    chunk_max_chars: int = 1000
    candidates: int = 20      # per retriever, before fusion
    rerank_pool: int = 10     # fused candidates scored by the cross-encoder
    top_k: int = 4
    # keyword | dense | hybrid | hybrid_rerank. Dense won with this embedder: BM25 fusion lowered Hit@1 (74% -> 70%),
    # and the reranker added +3 pts Hit@4 (2 questions, within noise) for ~80x the latency. See evals/reports/.
    retrieval_mode: str = os.getenv("RETRIEVAL_MODE", "dense")

    # --- Agent ---
    max_agent_steps: int = 8
    history_turns: int = 10   # full turns (incl. tool calls) kept verbatim in context

    # --- Business ---
    store_name: str = "Amazon.in"
    currency: str = os.getenv("CURRENCY", "₹")
    refund_approval_threshold: float = _float("REFUND_APPROVAL_THRESHOLD", "10000")

    # --- Paths / service ---
    db_path: Path = Path(os.getenv("DB_PATH", str(ROOT / "shopassist.db")))
    kb_dir: Path = ROOT / "knowledge_base" / "amazon_in"
    index_dir: Path = ROOT / "index"
    admin_api_key: str = os.getenv("ADMIN_API_KEY", "dev-admin-key")

    # --- Public demo protection (a free-tier key has ~100 LLM requests/day across the whole chain) ---
    demo_mode: bool = os.getenv("DEMO_MODE", "1") == "1"            # support desk open, data refreshed daily
    demo_access_code: str = os.getenv("DEMO_ACCESS_CODE", "")       # set it on a public deployment
    demo_daily_turn_cap: int = int(os.getenv("DEMO_DAILY_TURN_CAP", "150"))
    demo_turns_per_conversation: int = int(os.getenv("DEMO_TURNS_PER_CONVERSATION", "20"))
    demo_requests_per_ip_per_min: int = int(os.getenv("DEMO_IP_RPM", "10"))
    # Origins allowed to call the API from a browser when the UI is hosted elsewhere (e.g. on Vercel).
    cors_origins: tuple = tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip())


settings = Settings()


def today() -> date:
    """Single clock for policies, seed data and evals. STORE_DATE pins it for reproducible demos."""
    pinned = os.getenv("STORE_DATE")
    return date.fromisoformat(pinned) if pinned else date.today()


def money(amount: float) -> str:
    return f"{settings.currency}{amount:,.0f}"
