# Photo Sharing System — System Design

> **Scale:** 500M users · 1M DAU · 2M photos/day · ~1.02 PB over 7 years · 200ms read SLA

---

## 🗺️ Interactive Architecture (Miro Board)

The full system design is visualised as an interactive diagram on Miro — including all service layers, data flows, and storage tiers with live annotations.

**[👉 Open Miro Board](https://miro.com/app/board/uXjVG2ccVFQ=/)**

> The Miro board contains: Client/Frontend Layer → API/Backend Layer → Data Stores (PostgreSQL · Redis · Elasticsearch) → Object Storage Tiers (HOT · WARM · COLD) → Migration Worker, with all 15 labelled connectors, capacity callouts, design pattern summaries, and API reference.

---

## Table of Contents
1. [Capacity Estimation](#1-capacity-estimation)
2. [Database & Storage Choices](#2-database--storage-choices)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Upload Flow](#4-upload-flow)
5. [View Photo Flow (Cache-Aside)](#5-view-photo-flow-cache-aside)
6. [Search Flow](#6-search-flow)
7. [Download Flow](#7-download-flow)
8. [Storage Tier Migration Flow](#8-storage-tier-migration-flow)
9. [Database Schema](#9-database-schema)
10. [Key Design Patterns](#10-key-design-patterns)
11. [Storage Tiering & Cost](#11-storage-tiering--cost)
12. [API Reference](#12-api-reference)

---

## 1. Capacity Estimation

> Always start here in a system design interview. Numbers drive every architectural decision.

| Metric | Calculation | Result |
|--------|-------------|--------|
| Write throughput (avg) | 2,000,000 ÷ 86,400 | **~23 photos/sec** |
| Write throughput (peak 3×) | 23 × 3 | **~70 photos/sec** |
| Read throughput (avg) | 23 × 100 (read:write ratio) | **~2,300 reads/sec** |
| Read throughput (peak) | 2,300 × 3 | **~6,900 reads/sec** |
| Storage per day | 2M × 200 KB | **400 GB/day** |
| Storage per year | 400 GB × 365 | **~146 TB/year** |
| **Storage over 7 years** | 146 TB × 7 | **~1.02 PB** |
| Metadata DB rows | 2M/day × 365 × 7 | **~5.1 billion rows** |
| Metadata DB size | 5.1B × 500 B/row | **~2.5 TB** |
| Peak read bandwidth | 2,300 × 200 KB | **~460 MB/s = 3.68 Gbps** |

**Key conclusions from the math:**
- **3.68 Gbps peak** → CDN is mandatory; the backend cannot absorb this bandwidth
- **5.1 billion rows** → table partitioning (or sharding) is required; a single unpartitioned table would be unusable
- **~1 PB** → storage tiering is critical; storing all photos in the hot tier costs ~$23K/month vs ~$9.3K/month with tiering

---

## 2. Database & Storage Choices

| Store | Role | Why Chosen | Why Not the Alternative |
|-------|------|-----------|------------------------|
| **PostgreSQL** | Photo metadata — source of truth | ACID transactions, rich secondary indexes, range partitioning by year, familiar SQL | Cassandra: overkill at 23 writes/sec; no ACID; limited secondary indexes; higher operational complexity |
| **MinIO / S3** | Photo binary blobs | 5× cheaper than RDS storage, native CDN integration, byte-range streaming, no DB VACUUM impact | DB BLOBs: balloon VACUUM time, no CDN, no streaming, 5× more expensive |
| **Redis** | Metadata cache (cache-aside) | Sub-millisecond reads vs ~5ms DB; LRU eviction matches photo access patterns; shared across all backend instances | In-process cache: not shared across horizontally-scaled instances |
| **Elasticsearch** | Full-text search on title + product_id | Inverted index gives O(1) word lookup; BM25 relevance scoring; fuzzy matching; scales horizontally | `LIKE '%q%'`: full sequential scan at 5B rows, no index possible, unranked results |

---

## 3. High-Level Architecture

```mermaid
graph TD
    Browser(["🌐 Browser / Client"])
    CDN["☁️ CDN\n(CloudFront — prod)"]
    Nginx["🔷 Nginx\n:3000\nStatic Frontend"]
    FastAPI["⚡ FastAPI\n:8000\nStateless API Tier\n(horizontally scalable)"]
    PG[("🗄️ PostgreSQL\n:5432\nMetadata\n5.1B rows, partitioned")]
    Redis[("⚡ Redis\n:6379\nMetadata Cache\nLRU 256MB, TTL 1h")]
    ES[("🔎 Elasticsearch\n:9200\nInverted Index\nBM25 · Fuzzy")]
    MinIO["🪣 MinIO / S3\n:9000"]
    HOT["🔥 photos-hot\nS3 Standard\n0–12 months"]
    WARM["🌡️ photos-warm\nS3 Standard-IA\n1–3 years · 40% cheaper"]
    COLD["🧊 photos-cold\nS3 Glacier\n3–7 years · 80% cheaper"]
    Worker["⚙️ Migration Worker\nDaily cron\nHOT→WARM→COLD"]

    Browser -->|"Static UI"| Nginx
    Browser -->|"REST API calls\n(CORS)"| FastAPI
    Browser -.->|"Direct download\nvia presigned URL\nbypasses FastAPI"| CDN
    CDN -.->|"Cache miss"| MinIO

    FastAPI -->|"Metadata R/W\npool_size=20"| PG
    FastAPI -->|"Cache-aside\nGET/SETEX/DEL"| Redis
    FastAPI -->|"Dual-write index\nBM25 search"| ES
    FastAPI -->|"upload / presigned URL\nstream bytes"| MinIO

    MinIO --> HOT
    MinIO --> WARM
    MinIO --> COLD

    Worker -->|"SELECT tier candidates\nUPDATE storage_tier"| PG
    Worker -->|"copy_object + remove_object\nacross buckets"| MinIO

    style Browser fill:#4a9eff,color:#fff
    style CDN fill:#ff9f43,color:#fff
    style Nginx fill:#6c63ff,color:#fff
    style FastAPI fill:#6c63ff,color:#fff
    style PG fill:#2e86de,color:#fff
    style Redis fill:#ff6b6b,color:#fff
    style ES fill:#00d4aa,color:#fff
    style MinIO fill:#8854d0,color:#fff
    style HOT fill:#ee5a24,color:#fff
    style WARM fill:#f9ca24,color:#333
    style COLD fill:#60a3bc,color:#fff
    style Worker fill:#576574,color:#fff
```

**Service dependency startup order** (from `docker-compose.yml`):
```
postgres (healthy) ─┐
redis    (healthy) ─┼──► backend ──► migration-worker
elasticsearch (healthy) ─┤
minio    (healthy) ──► minio-init (creates 3 buckets) ─┘
```

---

## 4. Upload Flow

> **File:** `backend/services/photo_service.py → upload_photo()`
> **Ordering principle:** MinIO upload first (durability before metadata). If MinIO fails, no orphan DB row.

### What happens and why

When a user uploads a photo, the system must guarantee **two things simultaneously**: the binary never gets lost (100% durability, NFR #4) and the metadata stays consistent with what's actually stored. The ordering of operations is intentional:

1. **Validate first, write never.** Before touching any storage, we reject invalid file types and oversized files at the edge. This avoids paying I/O cost for bad inputs.
2. **Object storage before the database.** MinIO receives the binary first. If this step fails (network blip, bucket quota), we haven't written a metadata row yet — so there's no dangling record pointing at a missing file. Reversing this order would leave orphan metadata rows on MinIO failure, causing broken photo links.
3. **PostgreSQL is the source of truth.** Once the binary is safely persisted, we write the canonical metadata row. At this point the photo officially "exists" in the system. The generated UUID from PostgreSQL becomes the stable external identifier.
4. **Elasticsearch is a replica, not required.** Indexing into Elasticsearch is done asynchronously with fire-and-forget semantics. If ES is temporarily down, the upload still succeeds — the photo is accessible, just not yet searchable. This is explicitly allowed by NFR #3 (eventual consistency). A background retry or re-index job can catch up later.
5. **Presigned URL returned immediately.** Rather than making the client do a separate round-trip to fetch the photo URL, we generate a 1-hour presigned MinIO URL and embed it in the 201 response. This lets the frontend render the uploaded photo instantly without a second request.

```mermaid
sequenceDiagram
    autonumber
    participant B as 🌐 Browser
    participant A as ⚡ FastAPI
    participant PG as 🗄️ PostgreSQL
    participant M as 🔥 MinIO (HOT)
    participant ES as 🔎 Elasticsearch

    B->>A: POST /api/photos/upload<br/>multipart(file, title, user_id, product_id?)

    A->>A: Validate content_type ∈ {jpeg,png,webp,gif}<br/>Validate size ≤ 10 MB

    A->>PG: SELECT * FROM users WHERE id = user_id
    PG-->>A: user row  ❌ 404 if not found

    Note over A,M: DURABILITY FIRST — upload binary before writing metadata
    A->>M: put_object(key=YYYY/MM/uuid.ext, bytes, content_type)
    M-->>A: ✅ OK

    A->>PG: INSERT INTO photos<br/>(id, user_id, title, product_id,<br/>file_path, size_bytes, storage_tier='HOT')
    PG-->>A: photo row + generated UUID

    Note over A,ES: Async — tolerates failure (NFR #3 eventual consistency)
    A-)ES: index_photo(id, title, product_id,<br/>storage_tier, created_at)
    ES--)A: indexed  ⚠️ failure logged, upload still succeeds

    A->>M: presigned_get_object(key, expires=1h)
    M-->>A: signed URL (time-limited, cryptographically signed)

    A-->>B: 201 Created<br/>{id, title, storage_tier="HOT", url, size_bytes}
```

**Why this order matters:**

| Step | Failure Mode | Impact |
|------|-------------|--------|
| MinIO before PostgreSQL | MinIO fails → no DB write | ✅ No orphan metadata |
| PostgreSQL before Elasticsearch | ES fails → photo still accessible | ✅ Search eventually consistent |
| If PostgreSQL fails after MinIO | Orphan object in MinIO | ⚠️ Acceptable — background cleanup in prod |

---

## 5. View Photo Flow (Cache-Aside)

> **File:** `backend/services/photo_service.py → get_photo()`
> **Pattern:** Cache-aside (lazy loading) with Redis TTL = 1 hour

### What happens and why

This is the most critical path in the system — at 2,300 reads/sec peak, every millisecond matters. The flow has two distinct paths depending on whether the metadata is already cached.

**Why cache metadata and not the photo binary?**
Photo binaries average 200 KB each. Caching them in Redis at 2,300 reads/sec would require terabytes of RAM. Instead, we cache only the metadata (≈500 bytes per photo) and let MinIO/CDN handle the binary. Redis holds the "address" of the photo, not the photo itself.

**Cache HIT path (~80% of requests):**
The request completes in approximately **2–5 ms**:
- Redis GET is a single O(1) key lookup in memory (~0.2ms)
- Presigned URL generation is a local HMAC-SHA256 signing operation (~1ms)
- Client gets back metadata + URL immediately, then downloads directly from MinIO

**Cache MISS path (~20% of requests):**
The request takes approximately **8–15 ms**:
- Redis returns nil — we fall through to PostgreSQL
- The DB query hits `idx_photos_id` (a unique index on UUID + partition key), so even at 5.1B rows it's a fast index seek, not a full scan
- The result is written back to Redis with a 1-hour TTL before returning — so the next request for the same photo will HIT

**Why DEL on invalidation, not SET?**
On photo update or tier change, we delete the Redis key rather than writing a new value. This avoids a race condition: if two writes happen concurrently, the slower one could overwrite the faster one's cached value with stale data. DEL forces the next read to go to PostgreSQL for the authoritative value.

**The presigned URL trick (the bandwidth bottleneck solution):**
The response contains a URL, not bytes. The client then fetches the photo directly from MinIO — the backend is completely out of the data transfer path. At 3.68 Gbps peak, routing image bytes through FastAPI would require dozens of large EC2 instances just for bandwidth. Presigned URLs reduce that to zero backend bandwidth cost. In production, CloudFront caches these presigned responses at edge nodes, achieving <50ms globally.

```mermaid
sequenceDiagram
    autonumber
    participant B as 🌐 Browser
    participant A as ⚡ FastAPI
    participant R as ⚡ Redis
    participant PG as 🗄️ PostgreSQL
    participant M as 🪣 MinIO

    B->>A: GET /api/photos/{photo_id}

    A->>R: GET photo:{photo_id}

    alt Cache HIT (~80% of requests)
        R-->>A: {metadata JSON} ✅ HIT<br/>increment cache_hits_total

        A->>M: presigned_get_object(file_path, tier, expires=1h)
        M-->>A: signed URL

        A-->>B: 200 {metadata + presigned_url}

        Note over B,M: Client downloads DIRECTLY from MinIO<br/>FastAPI not in the bandwidth path<br/>(eliminates 460 MB/s bottleneck at peak)
        B->>M: GET presigned_url (direct, no FastAPI)
        M-->>B: 📷 image bytes
    else Cache MISS (~20% of requests)
        R-->>A: nil ❌ MISS<br/>increment cache_misses_total

        A->>PG: SELECT * FROM photos WHERE id = photo_id
        PG-->>A: photo row  ❌ 404 if not found

        A->>R: SETEX photo:{photo_id} 3600 {metadata JSON}
        Note right of R: TTL = 1 hour<br/>LRU evicts cold photos automatically

        A->>M: presigned_get_object(file_path, tier, expires=1h)
        M-->>A: signed URL

        A-->>B: 200 {metadata + presigned_url}

        B->>M: GET presigned_url (direct, no FastAPI)
        M-->>B: 📷 image bytes
    end
```

**Cache-aside vs alternatives:**

| Pattern | Read path | Write path | When to use |
|---------|-----------|------------|-------------|
| **Cache-aside** ← this | Cache → miss → DB → populate | Write DB → invalidate cache | Read-heavy (100:1), resilient to cache failure |
| Write-through | Always from cache | Write DB + cache | Balanced read/write, always-warm cache |
| Write-behind | Always from cache | Write cache → async flush to DB | Write-heavy, tolerate data loss risk |

**Impact at scale:**
- 80% hit ratio at 2,300 reads/sec → only **460 DB reads/sec** (trivial for PostgreSQL)
- Redis 256 MB headroom ≫ ~50 MB needed for hot metadata

---

## 6. Search Flow

> **Files:** `backend/services/search_service.py`, `backend/services/photo_service.py → search_photos()`
> **Pattern:** Dual-write (PostgreSQL source of truth + Elasticsearch search replica)

### What happens and why

Search is the most architecturally interesting flow because it involves two entirely different storage systems working in tandem.

**Why can't we just use PostgreSQL for search?**
The naive approach — `SELECT * FROM photos WHERE title LIKE '%sneakers%'` — is catastrophically slow at 5.1 billion rows. A `LIKE '%keyword%'` with a leading wildcard cannot use any B-tree index. PostgreSQL must read every single row to check for a match. At 730M rows per year-partition, even a single-year scan would take minutes. Elasticsearch solves this with an **inverted index**: at index time, every word in every title is mapped to a list of document IDs. At search time, looking up "sneakers" is a single hash lookup — O(1) regardless of corpus size.

**The dual-write trade-off:**
On every upload, we write to both PostgreSQL (authoritative, ACID) and Elasticsearch (search replica, eventual consistency). This means:
- If ES write fails: the photo is stored and accessible, just not yet searchable. NFR #3 explicitly allows this gap.
- ES can be rebuilt from scratch at any time by re-scanning PostgreSQL — it's a derived view, not a primary store.
- In production, a message queue (Kafka/SQS) between the API and ES indexer gives guaranteed delivery with backpressure, at the cost of ~100ms indexing latency.

**BM25 relevance scoring:**
Elasticsearch uses BM25 (Best Match 25) — an industry-standard term-frequency/inverse-document-frequency algorithm. It ranks "Blue Sneakers Running Shoes" higher than "Shoes" for the query "sneakers" because the former mentions sneakers more prominently relative to document length. The `title^2` field boost means a title match scores twice as highly as a product_id match.

**Fuzzy matching:**
`fuzziness: "AUTO"` allows Elasticsearch to match "sneakres" → "sneakers" (edit distance 2). This is implemented via a Levenshtein automaton — not a full scan — so it stays fast.

**Why fetch file_path from PostgreSQL after the ES query?**
Elasticsearch stores only the fields needed for search + result rendering (id, title, product_id, score, tier). The `file_path` (MinIO object key) is not stored in ES — it's large and not needed for ranking. After ES returns the matching IDs, we do a small indexed UUID lookup in PostgreSQL per result. At 20 results per page, this is 20 fast primary-key lookups — negligible overhead compared to the ES query itself. An alternative is storing `file_path` in ES (denormalization) to avoid the PG roundtrip, at the cost of ES storage and keeping the two in sync on file moves.

```mermaid
sequenceDiagram
    autonumber
    participant B as 🌐 Browser
    participant A as ⚡ FastAPI
    participant ES as 🔎 Elasticsearch
    participant PG as 🗄️ PostgreSQL
    participant M as 🪣 MinIO

    B->>A: GET /api/photos/search<br/>?q=sneakers&product_id=SKU-1234&page=1&size=20

    A->>A: Validate: q or product_id must be present<br/>❌ 400 if both missing

    Note over A,ES: Elasticsearch inverted index: O(1) word lookup<br/>vs LIKE '%sneakers%' = full scan at 5.1B rows

    A->>ES: POST /photos/_search<br/>multi_match{<br/>  fields: [title^2, product_id],<br/>  query: "sneakers",<br/>  fuzziness: "AUTO",<br/>  type: "best_fields"<br/>}<br/>sort: [_score desc, created_at desc]<br/>from: 0, size: 20
    ES-->>A: {total:42, hits:[{id,title,product_id,score,storage_tier,created_at},...]}

    loop For each search result (up to 20)
        A->>PG: SELECT file_path FROM photos WHERE id = hit.id<br/>(indexed UUID lookup, very fast)
        PG-->>A: file_path

        A->>M: presigned_get_object(file_path, storage_tier, expires=1h)
        M-->>A: signed URL
    end

    A-->>B: 200 {total:42, page:1, size:20,<br/>results:[{id,title,score,url,...}]}
```

**Elasticsearch query anatomy:**
```json
{
  "query": {
    "bool": {
      "must": [
        {
          "multi_match": {
            "query": "sneakers",
            "fields": ["title^2", "product_id"],
            "fuzziness": "AUTO"
          }
        },
        { "term": { "product_id": "SKU-1234" } }
      ]
    }
  },
  "sort": [{ "_score": "desc" }, { "created_at": "desc" }],
  "from": 0, "size": 20
}
```

**Dual-write consistency trade-off:**
- PostgreSQL = authoritative (ACID)
- Elasticsearch = replica (eventual consistency, NFR #3 compliant)
- Gap window: seconds to minutes depending on retry strategy
- Production option: async indexing via Kafka/SQS for guaranteed delivery

---

## 7. Download Flow

> **File:** `backend/services/photo_service.py → download_photo()`
> **Note:** This endpoint streams bytes through the backend. For production scale, use the presigned URL from `GET /api/photos/{id}` instead.

### What happens and why

The download endpoint is intentionally simpler than the view flow — it streams raw bytes rather than returning a presigned URL. This serves a different use case.

**When would you use this over the presigned URL?**
- **Server-side processing:** a backend job that needs to read and transform the photo (resize, watermark, format convert) — it can call this endpoint without needing to follow a redirect
- **Download logging:** if you need to record every download with user identity, timestamp, and IP for audit or billing, streaming through the backend gives you a hook. With presigned URLs, the client hits MinIO directly and the backend never sees the traffic.
- **Internal service-to-service calls:** microservices inside the same VPC that don't need public presigned URL signatures

**The storage tier resolution:**
The `storage_tier` field in PostgreSQL tells us exactly which MinIO bucket to look in. HOT photos are in `photos-hot`, WARM in `photos-warm`, COLD in `photos-cold`. The backend resolves the correct bucket before calling MinIO — the client never needs to know which tier a photo is in.

**Range request support:**
MinIO's `get_object` supports HTTP Range headers, which means a client can request a specific byte range: `Range: bytes=0-1023`. This enables resumable downloads (restart from where you left off) and is essential for video/large file support. The byte stream is piped directly to the response without buffering in memory.

**The bandwidth warning:**
At 460 MB/s average read bandwidth, routing all downloads through this endpoint would saturate the backend's network interface. A single `c5.2xlarge` EC2 instance has ~625 MB/s network throughput — meaning this single endpoint could max out an entire instance under average load, leaving nothing for upload, search, and view APIs. This is why the presigned URL pattern exists: let MinIO (which scales storage bandwidth independently) absorb the data transfer.

```mermaid
sequenceDiagram
    autonumber
    participant B as 🌐 Browser
    participant A as ⚡ FastAPI
    participant PG as 🗄️ PostgreSQL
    participant M as 🪣 MinIO

    B->>A: GET /api/photos/{photo_id}/download

    A->>PG: SELECT file_path, content_type, storage_tier<br/>FROM photos WHERE id = photo_id
    PG-->>A: file_path, content_type, storage_tier  ❌ 404 if not found

    Note over A,M: Backend resolves correct bucket from storage_tier:<br/>HOT→photos-hot | WARM→photos-warm | COLD→photos-cold

    A->>M: get_object(bucket=photos-{tier}, object=file_path)
    M-->>A: 📷 byte stream (supports Range requests)

    A-->>B: 200 binary response<br/>Content-Type: image/jpeg<br/>Content-Disposition: attachment; filename={photo_id}

    Note over A,M: ⚠️ Backend in bandwidth path here<br/>At 460 MB/s peak this is a bottleneck<br/>Use presigned URL (GET /{id}) for production
```

**Streaming vs Presigned URL trade-off:**

| Approach | Bandwidth | Use case |
|----------|-----------|----------|
| **Stream through backend** ← this | Backend absorbs all bandwidth | Server-side access control, download logging |
| **Presigned URL** (`GET /{id}`) | Zero backend bandwidth | Production at scale — CDN caches the URL |

---

## 8. Storage Tier Migration Flow

> **Files:** `backend/services/tier_migration.py`, `backend/scripts/tier_migration_cron.py`
> **Schedule:** Every 24 hours (configurable via `MIGRATION_INTERVAL_SECONDS`)

### What happens and why

The migration worker is the cost optimization engine of the system. It runs independently of the API tier and silently moves photos between storage tiers based on access recency — without any user-facing disruption.

**Why does this exist?**
All photos start in the HOT tier (S3 Standard, $0.023/GB). Without migration, 7 years of photos accumulate there and the monthly bill grows linearly to ~$23,460/month. The observation is that photo access follows a power law: photos get most of their views in the first few weeks after upload, then traffic decays sharply. A photo not accessed in 12 months is statistically very unlikely to be accessed again at the same rate. Moving it to a cheaper tier costs nothing in user experience (retrieval is still fast from WARM; only COLD has a multi-hour retrieval delay).

**The decision signal: `last_accessed_at`:**
Every time a photo is viewed or downloaded, its `last_accessed_at` timestamp is updated in PostgreSQL. The migration job queries this field to identify candidates. A photo is eligible for HOT→WARM if it hasn't been accessed in 365 days. WARM→COLD requires 1,095 days (3 years). These thresholds are configurable via environment variables so the team can tune the cost/latency trade-off without code changes.

**Why batch processing (LIMIT 100)?**
Processing all eligible photos in a single transaction would create extremely long-running DB transactions, lock rows for extended periods, and risk timeout or OOM. Batching in groups of 100 keeps transactions short, allows the worker to make incremental progress, and means a crash loses at most 100 photos' worth of in-flight migration work (which is safe — the DB still shows their old tier, so the next run retries them).

**The copy-then-verify-then-delete safety protocol:**
Object storage (S3/MinIO) has no atomic rename or move primitive. The only way to "move" an object is to copy it and then delete the source. This creates a window where the object exists in both places. The verification step (`stat_object`) before deletion closes the critical gap: if the copy failed silently or partially, the stat call will detect it and we abort — leaving the source intact. If the worker crashes after the copy but before the delete, the next run finds the DB still shows the old tier, re-attempts the copy (which is idempotent — same key, same bucket), re-verifies, and cleans up the source. No data loss in any crash scenario.

**Why update the DB before deleting the source?**
The DB record (`storage_tier='WARM'`) is updated before `remove_object` on the source. This means if the delete fails (transient network error), the DB says WARM but the object exists in both HOT and WARM. The next migration run will try to copy again (idempotent) and eventually delete the HOT copy. The alternative — delete source first, then update DB — is much worse: a crash between those two steps would leave the DB saying HOT but the object only in WARM, causing failed reads from the HOT bucket.

**Operational independence:**
The migration worker runs as a completely separate Docker service (`migration-worker`). It can be paused, scaled down to zero, or restarted without affecting the API. In production this would be a Kubernetes CronJob or AWS EventBridge rule triggering a Lambda — no persistent infrastructure needed outside the execution window.

```mermaid
sequenceDiagram
    autonumber
    participant W as ⚙️ MigrationWorker
    participant PG as 🗄️ PostgreSQL
    participant H as 🔥 MinIO (HOT)
    participant WM as 🌡️ MinIO (WARM)
    participant C as 🧊 MinIO (COLD)

    Note over W: Wakes every 24h<br/>HOT_TO_WARM_DAYS=365<br/>WARM_TO_COLD_DAYS=1095

    W->>PG: SELECT id, file_path FROM photos<br/>WHERE storage_tier='HOT'<br/>AND last_accessed_at < NOW() - 365 days<br/>LIMIT 100
    PG-->>W: [{id, file_path}, ...] (batch of candidates)

    loop HOT → WARM (for each photo in batch)
        W->>WM: copy_object(source=photos-hot/key,<br/>dest=photos-warm/key)
        WM-->>W: ✅ OK

        W->>WM: stat_object(photos-warm, key)
        Note right of W: Verify copy before deleting source<br/>(no atomic move in object storage)
        WM-->>W: ✅ exists

        W->>PG: UPDATE photos SET storage_tier='WARM'<br/>WHERE id = photo_id
        PG-->>W: ✅ OK

        W->>H: remove_object(photos-hot, key)
        H-->>W: ✅ OK
    end

    W->>PG: SELECT id, file_path FROM photos<br/>WHERE storage_tier='WARM'<br/>AND last_accessed_at < NOW() - 1095 days<br/>LIMIT 100
    PG-->>W: [{id, file_path}, ...] (batch of candidates)

    loop WARM → COLD (for each photo in batch)
        W->>C: copy_object(source=photos-warm/key,<br/>dest=photos-cold/key)
        C-->>W: ✅ OK

        W->>C: stat_object(photos-cold, key)
        C-->>W: ✅ exists

        W->>PG: UPDATE photos SET storage_tier='COLD'<br/>WHERE id = photo_id
        PG-->>W: ✅ OK

        W->>WM: remove_object(photos-warm, key)
        WM-->>W: ✅ OK
    end

    W->>W: Log summary<br/>{hot_to_warm: N, warm_to_cold: M, errors: E}
```

**Copy-then-delete safety rationale:**
Object storage has no atomic rename/move. The sequence must be:
1. **Copy** → destination bucket
2. **Verify** → `stat_object` confirms copy exists
3. **Update DB** → source of truth updated
4. **Delete** → source object removed

If the worker crashes between steps 2 and 4, the object exists in both buckets (wasted space, not data loss). On next run, the DB still shows old tier, so migration retries safely.

---

## 9. Database Schema

```mermaid
graph LR
    subgraph PostgreSQL["🗄️ PostgreSQL — Partitioned Schema"]
        U["users\n──────\nid UUID PK\nusername VARCHAR(50) UNIQUE\nemail VARCHAR(255) UNIQUE\ncreated_at TIMESTAMPTZ"]

        P["photos  PARTITION BY RANGE(created_at)\n──────────────────────────────────────\nid UUID NOT NULL\nuser_id UUID FK→users.id\ntitle VARCHAR(500)\nproduct_id VARCHAR(100)  ← nullable\nfilename VARCHAR(255)\nfile_path TEXT  ← MinIO object key\nsize_bytes BIGINT\ncontent_type VARCHAR(100)\nstorage_tier VARCHAR(10) ∈ {HOT,WARM,COLD}\ncreated_at TIMESTAMPTZ\nlast_accessed_at TIMESTAMPTZ"]

        P24["photos_2024\n2024-01-01 → 2025-01-01"]
        P25["photos_2025\n2025-01-01 → 2026-01-01"]
        P26["photos_2026\n2026-01-01 → 2027-01-01"]
        PD["photos_default\n(catch-all)"]

        AL["photo_access_log\n──────\nid BIGSERIAL PK\nphoto_id UUID\naccessed_at TIMESTAMPTZ\nsource VARCHAR(20)"]

        U -->|"1 : many"| P
        P --> P24
        P --> P25
        P --> P26
        P --> PD
        P -->|"append-only log"| AL
    end
```

**Indexes and their query patterns:**

| Index | Columns | Type | Query Pattern |
|-------|---------|------|---------------|
| `idx_photos_id` | `(id, created_at)` | UNIQUE | Fetch photo by UUID |
| `idx_photos_user_created` | `(user_id, created_at DESC)` | B-tree composite | User gallery pagination |
| `idx_photos_product_id` | `(product_id, created_at DESC) WHERE product_id IS NOT NULL` | **Partial** | Product photo lookup |
| `idx_photos_tier_accessed` | `(storage_tier, last_accessed_at)` | B-tree | Migration cron candidates |

**Why time-based partitioning?**

| Operation | Without Partitioning | With Partitioning |
|-----------|---------------------|-------------------|
| `WHERE created_at > '2026-01-01'` | Scan 5.1B rows | Scan only `photos_2026` (730M rows) |
| Archive 2024 data | `DELETE` 730M rows (hours) | `DROP TABLE photos_2024` (milliseconds) |
| VACUUM / ANALYZE | Full 5.1B row table | Per-partition only |
| Index rebuild | Full 5.1B row table | Per-partition only |

---

## 10. Key Design Patterns

### Cache-Aside (Lazy Loading)
**File:** `backend/services/cache_service.py`

```
Read:   Redis GET → HIT: return | MISS: DB SELECT → Redis SETEX TTL=3600 → return
Write:  DB INSERT → Redis DEL (invalidate, not overwrite)
```
Choosing DEL over SET on write avoids the race condition where a stale cached value overwrites a fresher DB value during concurrent writes.

---

### Presigned URL (Bandwidth Elimination)
**File:** `backend/services/storage_service.py`

```
Client → FastAPI → [DB lookup + URL generation, ~2-8ms] → return presigned URL
Client → MinIO DIRECTLY [download at full S3 speed, bypasses FastAPI]
```
At 460 MB/s peak, routing bytes through FastAPI would require 23× the server capacity. Presigned URLs solve this at zero backend cost.

---

### Dual-Write (PostgreSQL + Elasticsearch)
**File:** `backend/services/photo_service.py → upload_photo()`

```
Upload: PostgreSQL INSERT (ACID, source of truth)
        → Elasticsearch index (async, tolerates failure)
Search: Elasticsearch query → PostgreSQL file_path lookup
```
Consistency window = time between ES failure and retry. Acceptable per NFR #3 (eventual consistency).

---

### Time-Based Table Partitioning
**File:** `backend/db/migrations/001_initial.sql`

```sql
-- One partition per year — DROP TABLE for instant archival
CREATE TABLE photos_2024 PARTITION OF photos
  FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
```
At 7 years × 2M photos/day, this is the difference between a 5.1B-row monolith and 7 manageable ~730M-row partitions with automatic query pruning.

---

### Copy-Then-Delete (No Atomic Move in Object Storage)
**File:** `backend/services/storage_service.py → move_to_tier()`

```
copy_object(src, dst) → stat_object(dst) [verify] → UPDATE DB tier → remove_object(src)
```
S3 / MinIO has no rename primitive. This four-step sequence with a verification guard ensures data integrity even if the worker crashes mid-migration.

---

### Background Worker (Separation of Concerns)
**File:** `backend/scripts/tier_migration_cron.py`

The migration worker is a fully independent service with its own container. API failures don't affect migration. Migration slowdowns don't affect API latency. Each can be scaled, restarted, or scheduled independently.

---

## 11. Storage Tiering & Cost

| Tier | Age | Bucket | S3 Equivalent | $/GB/month | Estimated Data | Monthly Cost |
|------|-----|--------|---------------|-----------|----------------|-------------|
| 🔥 HOT | 0–12 months | `photos-hot` | S3 Standard | $0.023 | ~146 TB | ~$3,358 |
| 🌡️ WARM | 1–3 years | `photos-warm` | S3 Standard-IA | $0.0125 | ~292 TB | ~$3,650 |
| 🧊 COLD | 3–7 years | `photos-cold` | S3 Glacier | $0.004 | ~584 TB | ~$2,336 |
| | | | **Total with tiering** | | **~1.02 PB** | **~$9,344/month** |
| | | | Without tiering (all HOT) | $0.023 | ~1.02 PB | **~$23,460/month** |

**Annual savings from tiering: ~$169,000**

Migration thresholds (configurable in `.env`):
- `HOT_TO_WARM_DAYS=365` — not accessed in 1 year → move to WARM
- `WARM_TO_COLD_DAYS=1095` — not accessed in 3 years → move to COLD

---

## 12. API Reference

| Method | Path | Description | Key Request Fields |
|--------|------|-------------|-------------------|
| `POST` | `/api/photos/upload` | Upload a photo | `file` (multipart), `title`, `user_id`, `product_id?` |
| `GET` | `/api/photos/{id}` | Get metadata + presigned URL | path `photo_id` (UUID) |
| `GET` | `/api/photos/{id}/download` | Stream raw photo bytes | path `photo_id` (UUID) |
| `GET` | `/api/photos/search` | Elasticsearch full-text search | `q?`, `product_id?`, `page`, `size` |
| `GET` | `/health` | All dependency health status | — |
| `GET` | `/metrics` | Prometheus-format metrics | — |
| `GET` | `/` | Service info + endpoint map | — |

**Services & Ports (local dev):**

| Service | URL | Credentials |
|---------|-----|-------------|
| Frontend UI | http://localhost:3000 | — |
| API + Swagger docs | http://localhost:8000/docs | — |
| MinIO Console | http://localhost:9001 | `minioadmin` / `minioadmin123` |
| PostgreSQL | `localhost:5432` | `photouser` / `photopass` / `photodb` |
| Redis | `localhost:6379` | — |
| Elasticsearch | http://localhost:9200 | — |

---

*Generated for [hegde86vinay/photo-system](https://github.com/hegde86vinay/photo-system)*
