"""Select active memories for one turn by lexical and semantic rank."""

from __future__ import annotations

import logging
import re
import sqlite3
from math import sumprod
from typing import TYPE_CHECKING

import jieba

from ..providers import EmbeddingProvider, HumanMessage, ProviderError
from ..session.tokens import estimate_message_tokens

if TYPE_CHECKING:
    from .store import MemoryStore

logger = logging.getLogger(__name__)

# Initial cutoff for cosine similarity; relevance depends on the embedding model.
_MIN_COSINE = 0.52
_TOP_K = 20
_BATCH_SIZE = 10  # DashScope text-embedding-v4 accepts ten documents per call.


_WORDS = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*|[\u3400-\u9fff]+")
_STOP = frozenset(
    ["什么", "如何", "怎么", "怎样", "请", "请问", "谢谢", "继续", "之前", "我们", "一下", "的", "了", "吗", "呢", "a", "an", "the", "is", "are", "in", "of", "on", "to", "do", "we", "what", "which", "how", "please", "thanks", "continue"]
)


# Exclude the whole turn so an assistant or tool echo cannot retain a detected secret.
_CREDENTIAL = re.compile(
    r"""(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)[\\"']*\s*[:=]"""
    r"|\bBearer\s+[a-z0-9._~+/=-]+|\bsk-[a-z0-9_-]{20,}|-----BEGIN[ A-Z]*PRIVATE KEY-----"
)

def contains_credentials(text: str) -> bool:
    """Detect common credential patterns before retaining a turn or sending a query for embedding."""
    return bool(_CREDENTIAL.search(text))


def keywords(text: str) -> list[str]:
    """Generate shared index and query terms, including full code identifiers and their components."""
    terms = []
    for word in _WORDS.findall(text.casefold()):
        if word.isascii():
            terms.append(word)
            if "." in word or "_" in word:
                terms.extend(part for part in re.split(r"[._]", word) if part)
        else:
            terms.extend(jieba.cut_for_search(word))
    return [term for term in terms if term not in _STOP]


def _embedding_input(text: str) -> str:
    """Limit input to 8192 UTF-8 bytes while retaining complete characters."""
    # Bound even a single long code block or a multi-byte query.
    return text.encode("utf-8")[:8192].decode("utf-8", errors="ignore")


class MemoryRecall:
    def __init__(self, store: MemoryStore, provider: EmbeddingProvider | None = None) -> None:
        """Use the workspace store for lexical recall and an optional provider for semantic recall."""
        self._store = store
        self._provider = provider

    async def index_missing(self) -> None:
        """Backfill active entries for the current embedding model in batches until exhausted or unavailable."""
        provider = self._provider
        if provider is None:
            return
        while batch := self._store.missing_embeddings(provider.model_key, _BATCH_SIZE):
            ids, texts = zip(*batch)
            try:
                vectors = await provider.embed([_embedding_input(text) for text in texts])
            except ProviderError:
                logger.warning("Memory embedding batch unavailable (count=%d)", len(batch))
                return
            self._store.save_embeddings(provider.model_key, ids, vectors)

    async def _semantic_hits(self, query: str) -> list[int]:
        """Return active IDs ranked by cosine similarity in the configured model's vector space."""
        provider = self._provider
        if provider is None or contains_credentials(query):
            return []
        vectors = self._store.active_vectors(provider.model_key)
        if not vectors:
            return []
        try:
            (query_vector,) = await provider.embed([_embedding_input(query)])
        except ProviderError:
            logger.warning("Query embedding unavailable, using keyword recall")
            return []
        scores = []
        for memory_id, vector in vectors.items():
            if len(vector) == len(query_vector):
                cosine = sumprod(vector, query_vector)
                if cosine >= _MIN_COSINE:
                    scores.append((cosine, memory_id))
        return [memory_id for _, memory_id in sorted(scores, reverse=True)[:_TOP_K]]

    async def recall(self, query: str, *, token_budget: int = 1024) -> str:
        """Fill the prompt budget with user preferences and reciprocal-rank-fused lexical and semantic hits."""
        ranks: dict[int, float] = {}
        try:
            if keywords(query):
                lexical = self._store.search_keywords(query, _TOP_K)
                semantic = await self._semantic_hits(query)
                for hits in (lexical, semantic):
                    for rank, memory_id in enumerate(hits, start=1):
                        ranks[memory_id] = ranks.get(memory_id, 0.0) + 1 / (60 + rank)
            # Re-check active state after the network await: a correction may have arrived.
            active = {item.id: item for item in self._store.active_memories()}
        except (sqlite3.Error, OSError):
            logger.warning("Unable to read workspace memory for recall")
            return ""

        preferences = [item.id for item in active.values() if item.scope == "user" and item.kind == "preference"]
        preference_budget = min(token_budget, 128)
        lines = []
        selected = set()
        for memory_id in preferences:
            item = active[memory_id]
            line = f"- [{item.kind}] {item.text}"
            cost = estimate_message_tokens(HumanMessage(line))
            if cost <= preference_budget:
                lines.append(line)
                selected.add(memory_id)
                preference_budget -= cost
                token_budget -= cost
        for memory_id in sorted(ranks, key=lambda i: (ranks[i], i), reverse=True):
            if memory_id not in active or memory_id in selected:
                continue
            item = active[memory_id]
            line = f"- [{item.kind}] {item.text}"
            cost = estimate_message_tokens(HumanMessage(line))
            if cost <= token_budget:
                lines.append(line)
                token_budget -= cost
        return "\n".join(lines)
