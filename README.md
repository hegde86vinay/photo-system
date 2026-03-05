# Photo Sharing System — System Design Learning App

A production-grade photo sharing system built to teach **system design interview concepts** through working code. Every architectural decision maps directly to a standard interview talking point.

**Scale:** 500M users · 2M photos/day · ~1 PB storage · 7-year retention · 200ms read latency

---

## Quick Start

```bash
# 1. Clone and navigate
cd photo-system

# 2. Start all 8 services
docker-compose up --build

# Wait ~60 seconds for Elasticsearch to initialize, then:
# Frontend UI:   http://localhost:3000
# API Docs:      http://localhost:8000/docs
# MinIO Console: http://localhost:9001  (minioadmin / minioadmin123)
# Health check:  http://localhost:8000/health
# Metrics:       http://localhost:8000/metrics

# 3. Run the capacity calculator (great for interview prep!)
docker-compose exec backend python /app/scripts/capacity_calc.py
```

---

## System Architecture

```
   Browser/Client
        │
        ▼
   ┌─────────┐
   │  Nginx  │  Static frontend (CDN-able)
   │ :3000   │
   └────┬────┘
        │ REST API
        ▼
   ┌──────────┐
   │  FastAPI │  Stateless app tier (horizontally scalable)
   │  :8000   │
   └────┬─────┘
        │
   ┌────┼────────────────────────┐
   │    │                        │
   ▼    ▼               ▼        ▼
┌──────┐ ┌──────┐ ┌──────────┐ ┌──────────────────┐
│  PG  │ │Redis │ │    ES    │ │  MinIO (3 tiers) │
│:5432 │ │:6379 │ │  :9200   │ │ HOT  WARM  COLD  │
└──────┘ └──────┘ └──────────┘ └──────────────────┘
```

---

## System Design Interview Guide

### Step 1: Always Start with Capacity Math

Before drawing any boxes, do the math. Interviewers expect this.

| Metric | Calculation | Result |
|---|---|---|
| Write throughput | 2M photos/day ÷ 86,400 | **23 photos/sec** |
| Peak writes | 23 × 3× spike factor | **~70 photos/sec** |
| Read throughput | 23 × 100 (read:write ratio) | **2,300 reads/sec** |
| Storage per day | 2M × 200KB | **400 GB/day** |
| Storage per year | 400GB × 365 | **~146 TB/year** |
| **7-year total** | 146TB × 7 | **~1.02 PB** |
| Metadata DB rows | 2M/day × 7yr × 365 | **~5.1 billion rows** |
| Peak bandwidth | 2,300 req/sec × 200KB | **~460 MB/s = 3.68 Gbps** |

**Key insight from the math:**
- 3.68 Gbps peak bandwidth → **CDN is mandatory**, backend can't handle this
- 5.1B metadata rows → **table partitioning or sharding required**
- 1 PB storage → **tiering required** (storing all in S3 Standard = ~$23K/month)

---

### Step 2: Database Choice + Reasoning

#### Metadata Store: PostgreSQL ✓

**Why PostgreSQL over Cassandra?**

| Factor | PostgreSQL | Cassandra |
|---|---|---|
| ACID | ✓ Full | ✗ Eventual only |
| Secondary indexes | ✓ Rich, partial | Limited |
| Joins | ✓ | ✗ |
| Partitioning | ✓ Range/Hash | ✓ Token ring |
| Multi-region writes | Harder (primary-replica) | ✓ Native multi-master |
| Operational complexity | Lower | Higher |

**Choose PostgreSQL when:** ACID matters, complex queries needed, team knows SQL.
**Choose Cassandra when:** Multi-region active-active writes, very high write throughput (>100K/sec), simple access patterns.

For this system at 23 writes/sec, PostgreSQL is the right choice. Cassandra's complexity is not justified.

**Why not MySQL?** PostgreSQL has better JSON support, LISTEN/NOTIFY, table inheritance, and more mature partitioning — better for our evolving schema.

#### Photo Blobs: MinIO/S3 Object Storage ✓ (NOT the Database)

**Why never store binary blobs in PostgreSQL?**
1. **Cost**: S3 = $0.023/GB vs RDS = $0.115/GB (5× more expensive)
2. **VACUUM**: PostgreSQL VACUUM on TOAST (blob) columns is extremely slow
3. **Bandwidth**: S3/MinIO can serve millions of concurrent downloads; DB cannot
4. **CDN**: S3 integrates natively with CloudFront; DB blobs don't
5. **Streaming**: S3 supports byte-range requests; DB can't stream partial content
6. **Throughput**: At 460 MB/s peak, object storage scales horizontally

#### Search: Elasticsearch ✓ (NOT PostgreSQL LIKE)

```sql
-- This kills performance at 5 billion rows:
SELECT * FROM photos WHERE title LIKE '%sneakers%';
-- Full sequential scan, no index possible

-- Elasticsearch inverted index: O(1) lookup
-- "sneakers" → [photo_id_1, photo_id_4, photo_id_9, ...]
```

**Elasticsearch advantages:**
- Inverted index for O(1) word lookup regardless of corpus size
- BM25 relevance scoring (better than LIKE)
- Fuzzy matching ("sneakres" → "sneakers")
- Analyzer pipelines (stemming: "running" → "run")
- Horizontal scaling via shards

#### Metadata Cache: Redis ✓

At 2,300 reads/sec with 100:1 read:write ratio, PostgreSQL can handle it — but Redis drops DB load significantly:
- Sub-millisecond Redis vs ~5ms PostgreSQL
- 80% cache hit ratio → 460 DB reads/sec instead of 2,300
- LRU eviction matches photo access patterns (popular photos stay cached)
- TTL = 1 hour (acceptable inconsistency window per NFR #3)

---

### Step 3: Key Design Patterns Implemented

#### Cache-Aside Pattern (Lazy Loading)
```
Read:  Try Redis → HIT: return → MISS: read DB → populate cache → return
Write: Write DB → invalidate cache (not write-through)

Why not write-through?
  Read:write = 100:1. Write-through caches data that may never be read.
  Cache-aside only caches data that was actually requested.
  Also: cache-aside is resilient — if Redis is down, app continues from DB.
```

#### Presigned URL Pattern
```
Client ──GET /api/photos/{id}──→ Backend ──→ DB (metadata lookup)
Backend generates presigned URL (signed with MinIO secret, 1hr expiry)
Client ──GET presigned_url──→ MinIO DIRECTLY (bypasses backend!)

Why this matters:
  At 460 MB/s peak bandwidth, routing through the backend would OOM it.
  MinIO/S3 scales bandwidth independently.
  In production: presigned URL → CloudFront signed URL (CDN cached).
```

#### Dual-Write Pattern (PostgreSQL + Elasticsearch)
```
Upload:
  1. Write to PostgreSQL (source of truth)
  2. Write to Elasticsearch (search replica)

Risk: ES write fails → photo not searchable
Mitigation options (discuss in interview):
  A. Retry with exponential backoff (simple, used here)
  B. Async via message queue / Kafka (resilient, adds ~100ms latency)
  C. Periodic re-index from DB (simplest, eventual consistency only)

NFR #3 says eventual consistency is OK → option A is acceptable.
```

#### Table Partitioning (Time-based Sharding)
```sql
-- photos table: PARTITIONED BY RANGE (created_at)
-- photos_2024: 2024-01-01 to 2024-12-31
-- photos_2025: 2025-01-01 to 2025-12-31
-- ...
-- photos_2030: 2030-01-01 to 2030-12-31

Benefits:
  1. Partition pruning: "WHERE created_at > '2026-01-01'" scans only 1 partition
  2. Archival: DROP TABLE photos_2024 (instant vs. 730M DELETE rows)
  3. VACUUM: runs on current partition only (smaller, faster)
  4. Index rebuilds: per-partition, not full 5B-row table

Interview: "How would you shard?"
  Option A: By user_id hash     → even writes, good for user gallery queries
  Option B: By created_at time  → good for archival, write hotspot on current
  Option C: By product_id hash  → good if product search is primary pattern
  We chose time for 7-year archival demonstration.
```

---

### Step 4: 7-Year Data Retention Strategy

#### Storage Tiering (60% Cost Savings)

| Tier | Age | Bucket | S3 Equivalent | Price/GB | Monthly Cost |
|---|---|---|---|---|---|
| HOT | 0-12 months | photos-hot | S3 Standard | $0.023 | ~$3,358 |
| WARM | 1-3 years | photos-warm | S3 Standard-IA | $0.0125 | ~$3,650 |
| COLD | 3-7 years | photos-cold | S3 Glacier | $0.004 | ~$2,336 |

- **Without tiering (all HOT):** ~$23,460/month
- **With tiering:** ~$9,344/month
- **Annual savings:** ~$169,000

#### Migration Logic
```python
# Daily cron job (migration-worker service)
# HOT → WARM: not accessed in 365 days
# WARM → COLD: not accessed in 1,095 days (3 years)

# Move is copy-then-delete (object storage has no atomic rename):
# 1. Copy to destination bucket
# 2. Verify copy exists (stat check)
# 3. Update DB storage_tier field
# 4. Delete from source bucket
```

#### Database Archival (7 years)
```sql
-- Archive year 2024:
-- 1. pg_dump photos_2024 to S3 cold storage
-- 2. DROP TABLE photos_2024;  ← instant, no 730M row deletion
-- Much faster than: DELETE FROM photos WHERE created_at < '2025-01-01'
```

---

### Step 5: High Availability Design

**99.9% availability = max 8.7 hours downtime/year**

| Component | HA Strategy |
|---|---|
| PostgreSQL | Primary + Read Replicas (RDS Multi-AZ in production) |
| Redis | Redis Cluster or ElastiCache with failover |
| Elasticsearch | 3-node cluster, 1 replica per shard |
| MinIO/S3 | S3 cross-region replication (11 nines durability) |
| FastAPI | N stateless instances behind load balancer |
| CDN | Global edge nodes (CloudFront) |

**NFR #3 (Eventual Consistency):** If Redis is down, API falls back to PostgreSQL (slightly slower but correct). If Elasticsearch is down, search degrades but upload/view still works.

---

### Step 6: The 200ms Latency Requirement

**For image view/download:**
```
Without CDN:
  User (Sydney) → backend (US-East) → MinIO → return bytes
  = 150ms RTT + 50ms processing + 200KB transfer @ 1Gbps = ~201ms+ ✗

With CDN:
  User (Sydney) → CloudFront edge (Sydney) → serve cached presigned URL
  = ~30ms ✓

For presigned URL (metadata only, no bytes):
  L1: Redis hit = ~2ms response ✓ (easily < 200ms)
  L2: DB miss = ~8ms response ✓ (still < 200ms)
```

**Interview answer:** "We achieve 200ms via presigned URLs (client downloads from object storage directly), Redis caching for metadata (2ms vs 8ms DB), and CDN for global distribution. Backend never transfers photo bytes."

---

## API Reference

```bash
# Upload a photo
curl -X POST http://localhost:8000/api/photos/upload \
  -F "file=@photo.jpg" \
  -F "title=Blue Sneakers Front View" \
  -F "product_id=SKU-1234" \
  -F "user_id=00000000-0000-0000-0000-000000000001"

# Get photo by ID (Redis cache-aside + presigned URL)
curl http://localhost:8000/api/photos/{photo_id}

# Download photo bytes
curl -o photo.jpg http://localhost:8000/api/photos/{photo_id}/download

# Search (Elasticsearch)
curl "http://localhost:8000/api/photos/search?q=sneakers&product_id=SKU-1234"

# Health check (all dependencies)
curl http://localhost:8000/health

# Prometheus metrics (cache hit ratio, etc.)
curl http://localhost:8000/metrics

# Run capacity calculator
docker-compose exec backend python /app/scripts/capacity_calc.py

# Trigger tier migration manually
docker-compose exec backend python /app/scripts/tier_migration_cron.py
```

---

## What Each File Teaches

| File | System Design Concept |
|---|---|
| `docker-compose.yml` | Service mesh, dependency graphs, health checks, isolation |
| `db/migrations/001_initial.sql` | Table partitioning, partial indexes, UUID vs BIGSERIAL |
| `services/storage_service.py` | Object storage, presigned URLs, copy-then-delete move |
| `services/cache_service.py` | Cache-aside pattern, LRU, TTL, hit/miss metrics |
| `services/search_service.py` | Inverted index, dual-write, eventual consistency |
| `services/tier_migration.py` | Data lifecycle, cost optimization, batch processing |
| `services/photo_service.py` | Orchestration, failure modes, upload ordering |
| `api/health.py` | Observability, dependency health, SLO monitoring |
| `scripts/capacity_calc.py` | Interview math: storage, bandwidth, QPS estimation |
| `frontend/index.html` (Architecture tab) | System diagram, all concepts visualized |

---

## Services & Ports

| Service | URL | Credentials |
|---|---|---|
| Frontend UI | http://localhost:3000 | — |
| FastAPI backend | http://localhost:8000 | — |
| API Docs (Swagger) | http://localhost:8000/docs | — |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin123 |
| PostgreSQL | localhost:5432 | photouser / photopass / photodb |
| Redis | localhost:6379 | — |
| Elasticsearch | http://localhost:9200 | — |
