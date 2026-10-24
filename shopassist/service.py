"""Application service: the one entry point the API and the UI both use.
Owns sessions, persists masked transcripts + traces, and exposes the human-agent (admin) operations."""
import threading
import uuid
from dataclasses import asdict

from .agent import Session, SupportAgent, TurnResult
from .config import today
from .db.repository import Repository
from .db.seed import seed
from .guardrails import mask_pii, redact_secrets
from .retrieval import KnowledgeBase


class SupportService:
    def __init__(self, repo: Repository | None = None, kb: KnowledgeBase | None = None, agent: SupportAgent | None = None):
        self.repo = repo or Repository()
        self.kb = kb or KnowledgeBase()
        self.agent = agent or SupportAgent(self.repo, self.kb)
        # In-process session store. Multi-instance deployments would keep this in Redis.
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, threading.Lock] = {}

    def reset_demo_data(self):
        """Regenerate the demo database (dates are relative to today) and drop live sessions."""
        seed(self.repo.db_path)
        self._sessions.clear()
        self._locks.clear()

    def refresh_if_stale(self) -> bool:
        """A demo that runs for weeks would drift ('delivered 3 days ago' becomes 30). Re-seed once a day."""
        if self.repo.seeded_on() != today().isoformat():
            self.reset_demo_data()
            return True
        return False

    def start_conversation(self, customer_id: str) -> Session:
        customer = self.repo.get_customer(customer_id)
        if not customer:
            raise KeyError(f"Unknown customer {customer_id}")
        session = Session(id=uuid.uuid4().hex[:12], customer=customer)
        self.repo.create_conversation(session.id, customer_id)
        self._sessions[session.id] = session
        self._locks[session.id] = threading.Lock()
        return session

    def get_session(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise KeyError(f"Unknown conversation {session_id}")
        return self._sessions[session_id]

    def send(self, session_id: str, text: str) -> TurnResult:
        session = self.get_session(session_id)
        with self._locks[session_id]:  # one turn at a time per conversation (double-submit safe)
            result = self.agent.respond(session, text)
            stored_user = mask_pii(redact_secrets(text)[0])
            self.repo.add_message(session.id, "user", stored_user)
            self.repo.add_message(session.id, "assistant", mask_pii(result.reply))
            self.repo.save_trace(result.trace)
        return result

    # ---------- human-agent operations ----------
    def pending_approvals(self) -> list[dict]:
        return self.repo.pending_approvals()

    def decide_return(self, return_id: int, approve: bool, agent_name: str = "specialist") -> bool:
        return self.repo.decide_return(return_id, approve, agent_name)

    def tickets(self, status: str | None = "open") -> list[dict]:
        return self.repo.tickets(status)

    def resolve_ticket(self, ticket_id: int) -> bool:
        return self.repo.resolve_ticket(ticket_id)

    def metrics(self) -> dict:
        return self.repo.metrics()

    def conversations(self) -> list[dict]:
        return self.repo.conversations()

    def transcript(self, conversation_id: str) -> dict:
        return {"messages": self.repo.messages(conversation_id), "traces": self.repo.traces(conversation_id)}


def result_to_dict(result: TurnResult) -> dict:
    out = asdict(result)
    out["citations"] = [{k: c[k] for k in ("id", "title", "section", "url")} for c in result.citations]
    return out
