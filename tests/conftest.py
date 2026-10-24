import pytest

from shopassist.db.repository import Repository
from shopassist.db.seed import seed


class FakeKB:
    """Stands in for the real index so unit tests need no model downloads or API keys."""

    def __init__(self):
        self.queries = []
        self.chunks = [{"doc_id": "amazon-in-refund-policy"}]

    def search(self, query, k=None, mode="hybrid_rerank"):
        self.queries.append(query)
        return [{"hash": "h1", "title": "Amazon.in Refund Policy", "section": "Refund Timelines",
                 "url": "https://example.test/refunds", "text": "UPI Linked Bank Account | Up to 5 Working Days"}]


@pytest.fixture
def repo(tmp_path):
    return Repository(seed(tmp_path / "test.db"))


@pytest.fixture
def kb():
    return FakeKB()
