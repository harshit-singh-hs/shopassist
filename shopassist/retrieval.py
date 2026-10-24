"""Help-center retrieval over the crawled Amazon.in pages.

    markdown pages -> clean (boilerplate, nav sections, link URLs) -> heading-aware + table-aware chunks
    -> dense (bge-small, Qdrant) + sparse (BM25) -> Reciprocal Rank Fusion -> cross-encoder rerank
"""
import hashlib
import re
from functools import cached_property
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi

from .config import settings

MODES = ("keyword", "dense", "hybrid", "hybrid_rerank")
STOPWORDS = frozenset(
    "a an and are as at be by can do does did for from has have how i in is it its me my of on or our "
    "that the their this to was we were what when which who why will with you your".split()
)
NAV_HEADINGS = re.compile(r"^(table of contents|quick solutions|related (links|topics|help topics|articles)|"
                          r"need more help|more help|videos?)\b", re.I)
BOILERPLATE = re.compile(r"preferred language|English version will prevail|country flag", re.I)
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HEADING = re.compile(r"^(#{2,4})\s*(.*?)\s*$")


# ---------------- loading & cleaning ----------------
def parse_page(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    meta, body = {}, raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        for line in front.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    body = re.sub(r"^# .*\n", "", body.strip(), count=1)  # the H1 is the title, already in meta
    return {"doc_id": path.stem, "title": meta.get("title", path.stem), "url": meta.get("url", ""),
            "topic": meta.get("topic", ""), "fetched_at": meta.get("fetched_at", ""), "body": body}


def _is_navigation(text: str) -> bool:
    """Sections that are just lists of links ('Quick Solutions') carry no answerable content."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    links = [ln for ln in lines if re.fullmatch(r"[-*\d.]*\s*\[[^\]]+\]\([^)]*\)\.?", ln)]
    return bool(lines) and len(links) / len(lines) > 0.6


LIST_ITEM = re.compile(r"^\s*(?:\d+\.|[-*])\s")


def _unwrap(paragraph: str) -> str:
    """The source HTML is hard-wrapped; re-join wrapped lines but keep list items and table rows apart."""
    out = []
    for line in paragraph.splitlines():
        if out and not LIST_ITEM.match(line) and not line.lstrip().startswith("|") and not out[-1].lstrip().startswith("|"):
            out[-1] = f"{out[-1].rstrip()} {line.strip()}"
        else:
            out.append(line.rstrip())
    return "\n".join(out)


def _clean(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip() and not BOILERPLATE.search(p)]
    text = "\n\n".join(_unwrap(p) for p in paragraphs)
    text = MD_LINK.sub(lambda m: m.group(1), text)        # keep anchor text, drop (often huge) URLs
    text = text.replace("**", "")                          # emphasis markers are noise for retrieval
    return re.sub(r"[ \t]+", " ", text).strip()


def sections(page: dict) -> list[tuple[str, str]]:
    """Split on ##/###/#### keeping the heading path, e.g. 'Refund Timelines > Pay on Delivery'."""
    out, path, buf = [], {}, []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            heading = " > ".join(path[k] for k in sorted(path) if path[k]) or "Overview"
            out.append((heading, text))
        buf.clear()

    for line in page["body"].splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path = {k: v for k, v in path.items() if k < level}
            path[level] = MD_LINK.sub(lambda x: x.group(1), m.group(2)).strip(" :*")
        else:
            buf.append(line)
    flush()
    return [(h, t) for h, t in out if not NAV_HEADINGS.search(h.split(" > ")[-1]) and not _is_navigation(t)]


def _split_table(table: str, max_chars: int) -> list[str]:
    """Large tables are split by rows, repeating the header so every chunk stays interpretable."""
    rows = table.splitlines()
    header, body = rows[:2], rows[2:]
    parts, cur = [], []
    for row in body:
        if cur and len("\n".join(header + cur + [row])) > max_chars:
            parts.append("\n".join(header + cur))
            cur = []
        cur.append(row)
    if cur:
        parts.append("\n".join(header + cur))
    return parts


def _split_long(para: str) -> list[str]:
    """Oversized non-table paragraphs: split on list items first, then on sentences."""
    items = [i for i in re.split(r"\n(?=\s*(?:\d+\.|[-*])\s)", para) if i.strip()]
    if len(items) == 1:
        items = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", para)
    return [i.strip() for i in items if i.strip()]


def _pack(text: str, max_chars: int) -> list[str]:
    pieces = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        is_table = all(ln.lstrip().startswith("|") for ln in para.splitlines())
        if len(para) <= max_chars:
            pieces.append(para)
        else:
            pieces += _split_table(para, max_chars) if is_table else _split_long(para)
    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > max_chars:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def chunk_page(page: dict, max_chars: int) -> list[dict]:
    chunks = []
    for heading, raw_text in sections(page):
        text = _clean(raw_text)
        if len(text) < 40:
            continue
        for part in _pack(text, max_chars):
            chunks.append({
                "doc_id": page["doc_id"], "title": page["title"], "section": heading, "url": page["url"],
                "topic": page["topic"], "fetched_at": page["fetched_at"], "text": part,
                "hash": hashlib.sha1(f"{page['doc_id']}|{heading}|{part}".encode()).hexdigest(),
            })
    return chunks


def load_knowledge_base(kb_dir: Path | None = None) -> list[dict]:
    kb_dir = kb_dir or settings.kb_dir
    files = sorted(kb_dir.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No knowledge-base pages in {kb_dir}. Run: python -m scripts.fetch_kb")
    chunks = [c for f in files for c in chunk_page(parse_page(f), settings.chunk_max_chars)]
    for i, c in enumerate(chunks):
        c["id"] = i
    return chunks


def index_text(chunk: dict) -> str:
    """Contextual header: 'Amazon.in Refund Policy > Refund Timelines' travels with the text, so a
    table row that never says 'refund' is still found by refund queries."""
    return f"{chunk['title']} > {chunk['section']}\n{chunk['text']}"


# ---------------- ranking helpers ----------------
def tokenize(text: str) -> list[str]:
    """Lowercase tokens; compounds like 'pay-on-delivery' or 'a-to-z' are kept and also split."""
    tokens = []
    for tok in re.findall(r"[a-z0-9₹]+(?:[.\-][a-z0-9]+)*", text.lower()):
        if tok in STOPWORDS:
            continue
        tokens.append(tok)
        if "-" in tok or "." in tok:
            tokens += [p for p in re.split(r"[.\-]", tok) if p and p not in STOPWORDS]
    return tokens


def rrf(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score(d) = sum 1 / (k + rank_i(d)). Rank-based, so BM25 scores and
    cosine similarities (different scales) never need calibrating. k=60 from Cormack et al. 2009."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


# ---------------- store ----------------
class KnowledgeBase:
    def __init__(self, chunks: list[dict] | None = None, embed_model: str | None = None,
                 rerank_model: str | None = None):
        self.chunks = chunks if chunks is not None else load_knowledge_base()
        self.embed_model = embed_model or settings.embed_model
        self.rerank_model = rerank_model or settings.rerank_model
        self.embedder = TextEmbedding(self.embed_model)
        vectors = self._embed_with_cache()

        self.qdrant = QdrantClient(url=settings.qdrant_url) if settings.qdrant_url else QdrantClient(location=":memory:")
        if self.qdrant.collection_exists(settings.kb_collection):
            self.qdrant.delete_collection(settings.kb_collection)
        self.qdrant.create_collection(
            settings.kb_collection,
            vectors_config=models.VectorParams(size=vectors.shape[1], distance=models.Distance.COSINE),
        )
        self.qdrant.upsert(settings.kb_collection, points=[
            models.PointStruct(id=c["id"], vector=v.tolist(), payload={k: c[k] for k in ("doc_id", "title", "section", "topic")})
            for c, v in zip(self.chunks, vectors)
        ])
        self.bm25 = BM25Okapi([tokenize(index_text(c)) for c in self.chunks])

    def _embed_with_cache(self) -> np.ndarray:
        """Embeddings cached by chunk content hash: a KB refresh only re-embeds chunks that changed."""
        cache_path = settings.index_dir / f"kb_embeddings_{re.sub(r'[^a-z0-9]+', '_', self.embed_model.lower())}.npz"
        cache: dict[str, np.ndarray] = {}
        if cache_path.exists():
            data = np.load(cache_path)
            cache = dict(zip(data["hashes"].tolist(), data["vectors"]))
        missing = [c for c in self.chunks if c["hash"] not in cache]
        if missing:
            # Small batches bound peak memory (long-context models OOM'd at the default batch of 256).
            for c, v in zip(missing, self.embedder.passage_embed([index_text(c) for c in missing], batch_size=32)):
                cache[c["hash"]] = np.asarray(v, dtype=np.float32)
            settings.index_dir.mkdir(exist_ok=True)
            hashes = [c["hash"] for c in self.chunks]
            np.savez(cache_path, hashes=np.array(hashes), vectors=np.stack([cache[h] for h in hashes]))
        self.embedded_now = len(missing)
        return np.stack([cache[c["hash"]] for c in self.chunks])

    @cached_property
    def reranker(self) -> TextCrossEncoder:
        return TextCrossEncoder(self.rerank_model)

    def _dense(self, query: str, n: int) -> list[int]:
        qvec = next(iter(self.embedder.query_embed(query))).tolist()
        return [int(p.id) for p in self.qdrant.query_points(settings.kb_collection, query=qvec, limit=n).points]

    def _keyword(self, query: str, n: int) -> list[int]:
        scores = self.bm25.get_scores(tokenize(query))
        return [int(i) for i in np.argsort(-scores)[:n] if scores[i] > 0]

    def search(self, query: str, k: int | None = None, mode: str | None = None) -> list[dict]:
        k, mode = k or settings.top_k, mode or settings.retrieval_mode
        if mode == "keyword":
            ranked = [(i, None) for i in self._keyword(query, k)]
        elif mode == "dense":
            ranked = [(i, None) for i in self._dense(query, k)]
        elif mode in ("hybrid", "hybrid_rerank"):
            fused = rrf([self._dense(query, settings.candidates), self._keyword(query, settings.candidates)])
            if mode == "hybrid":
                ranked = fused[:k]
            else:
                pool = [i for i, _ in fused[:settings.rerank_pool]]
                # batch_size = pool size: one ONNX forward pass (~20% faster than the default on CPU).
                scores = self.reranker.rerank(query, [index_text(self.chunks[i]) for i in pool], batch_size=len(pool))
                ranked = sorted(zip(pool, map(float, scores)), key=lambda kv: kv[1], reverse=True)[:k]
        else:
            raise ValueError(f"mode must be one of {MODES}")
        return [{**self.chunks[i], "score": None if s is None else round(s, 4)} for i, s in ranked]
