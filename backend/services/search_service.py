"""
SYSTEM DESIGN CONCEPT: Search via Elasticsearch (Inverted Index)

WHY NOT PostgreSQL LIKE/ILIKE FOR SEARCH?
  LIKE '%keyword%' requires a full sequential scan — can't use B-tree index.
  At 5.1 billion rows, a full table scan for every search is unacceptable.

WHY ELASTICSEARCH?
  ES builds an inverted index: word → [doc_id, doc_id, ...]
  Lookup is O(1) regardless of corpus size.
  Supports relevance scoring (BM25), fuzzy matching, autocomplete, synonyms.

DUAL-WRITE PATTERN (interview must-know):
  Write path: DB (source of truth) → ES (search replica)
  Read path:  ES for search → DB for full metadata fetch

  Risk: DB write succeeds, ES write fails → search misses this photo.
  Mitigation options:
    1. Async ES indexing via message queue (most resilient, adds latency)
    2. Retry with exponential backoff (simple, may miss on crash)
    3. ES as async projection (batch re-index from DB on schedule)
  → We use option 2 here for simplicity; option 1 is the production answer.

EVENTUAL CONSISTENCY:
  NFR #3 allows eventual consistency. If a photo doesn't appear in search
  for a few seconds/minutes after upload, that's acceptable.
  This gives us flexibility to use async indexing without user impact.
"""
import uuid
from elasticsearch import AsyncElasticsearch
from config import get_settings

settings = get_settings()

INDEX_NAME = "photos"

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "id":          {"type": "keyword"},    # exact match only
            "title":       {"type": "text", "analyzer": "english"},   # tokenized, stemmed
            "product_id":  {"type": "keyword"},    # exact match (product IDs are codes, not prose)
            "user_id":     {"type": "keyword"},
            "size_bytes":  {"type": "long"},
            "storage_tier":{"type": "keyword"},
            "created_at":  {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,      # single-node dev setup; scale to 3+ in production
        "number_of_replicas": 0,    # set to 1+ in production for HA
    }
}


class SearchService:
    def __init__(self):
        self._client: AsyncElasticsearch | None = None

    def get_client(self) -> AsyncElasticsearch:
        if self._client is None:
            self._client = AsyncElasticsearch(hosts=[settings.es_url])
        return self._client

    async def ensure_index(self) -> None:
        """Idempotent: create index if it doesn't exist."""
        client = self.get_client()
        exists = await client.indices.exists(index=INDEX_NAME)
        if not exists:
            await client.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)

    async def index_photo(self, photo_id: uuid.UUID, title: str, product_id: str | None,
                          user_id: uuid.UUID, size_bytes: int, storage_tier: str,
                          created_at: str) -> None:
        """
        Index photo metadata in ES after successful DB insert (dual-write).
        CONCEPT: ES document = denormalized view of data optimized for search.
        We only store fields needed for search + result rendering here.
        Full metadata (file_path, content_type, etc.) lives in PostgreSQL.
        """
        client = self.get_client()
        await client.index(
            index=INDEX_NAME,
            id=str(photo_id),
            document={
                "id":           str(photo_id),
                "title":        title,
                "product_id":   product_id,
                "user_id":      str(user_id),
                "size_bytes":   size_bytes,
                "storage_tier": storage_tier,
                "created_at":   created_at,
            }
        )

    async def search(self, q: str | None, product_id: str | None,
                     page: int = 1, size: int = 20) -> tuple[int, list[dict]]:
        """
        Multi-match search on title + product_id fields.

        CONCEPT: Query anatomy
          multi_match: searches across multiple fields simultaneously
          "english" analyzer on title: stems words (running→run), removes stopwords
          product_id filter: exact keyword match (not analyzed)
          from/size: pagination (offset-based; for deep pagination use search_after)

        CONCEPT: Pagination trade-off
          Offset pagination (from/size): Simple but slow for deep pages (from=10000).
          Cursor pagination (search_after): Efficient for any depth, stateful.
          → We use offset here for simplicity; mention cursor in interview.
        """
        client = self.get_client()

        must_clauses = []

        if q:
            must_clauses.append({
                "multi_match": {
                    "query": q,
                    "fields": ["title^2", "product_id"],  # ^2 = boost title matches
                    "type": "best_fields",
                    "fuzziness": "AUTO",                  # typo tolerance: "phto"→"photo"
                }
            })

        if product_id:
            must_clauses.append({"term": {"product_id": product_id}})

        if not must_clauses:
            query = {"match_all": {}}
        else:
            query = {"bool": {"must": must_clauses}}

        response = await client.search(
            index=INDEX_NAME,
            body={
                "query": query,
                "from": (page - 1) * size,
                "size": size,
                "sort": [{"_score": "desc"}, {"created_at": "desc"}],
            }
        )

        total = response["hits"]["total"]["value"]
        hits = [
            {**hit["_source"], "score": hit["_score"]}
            for hit in response["hits"]["hits"]
        ]
        return total, hits

    async def ping(self) -> bool:
        try:
            client = self.get_client()
            return await client.ping()
        except Exception:
            return False
