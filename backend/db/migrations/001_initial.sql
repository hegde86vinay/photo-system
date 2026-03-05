-- ============================================================
-- SYSTEM DESIGN CONCEPTS IN THIS FILE:
--   1. UUID primary keys (distributed ID generation, no coordination needed)
--   2. Table partitioning by time (time-based sharding, archival, partition pruning)
--   3. Partial indexes (optimize read paths, reduce write overhead)
--   4. Composite indexes (cover multi-column query patterns)
--   5. Storage tier tracking (lifecycle management in metadata)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Users table ──────────────────────────────────────────────────────────────
-- CONCEPT: UUID vs BIGSERIAL
--   UUID: globally unique without coordination → safe for distributed inserts,
--         safe to generate client-side, no sequential scan attack surface.
--   BIGSERIAL: smaller (8B vs 16B), faster index, but requires central sequence.
--   At 23 inserts/sec, either works. At millions of concurrent writers across
--   data centers, UUID wins.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id          UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    username    VARCHAR(50)  NOT NULL UNIQUE,
    email       VARCHAR(255) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_email    ON users (email);
CREATE INDEX idx_users_username ON users (username);

-- ── Photos table: PARTITIONED BY RANGE (created_at) ──────────────────────────
--
-- CONCEPT: Range Partitioning (interview gold — always explain this clearly)
--
-- WHY PARTITION?
--   At 2M photos/day × 7 years = 5.11 billion rows.
--   A single table with 5B rows makes VACUUM, ANALYZE, and index rebuilds
--   extremely slow. Queries without time filters scan all 5B rows.
--
-- PARTITION BY TIME VS. BY USER_ID HASH:
--   Time-based: Great for archival (drop partition = instant delete of 1yr of data),
--               partition pruning on date ranges, write hotspot on current partition.
--   Hash by user_id: Even write distribution, good for "get all user photos" queries,
--                    harder to archive by age.
--   → We chose time-based to illustrate archival + storage tiering.
--
-- INTERVIEW TALKING POINT:
--   "We partition by created_at year. Each partition is ~730GB/year raw.
--    After 7 years, we have 7 partitions. Archiving year 2024:
--    1) pg_dump photos_2024 to S3
--    2) DROP TABLE photos_2024 (instant, no row-level delete)
--    3) Update MinIO tier accordingly.
--    This is 1 DDL statement vs. 730M DELETE rows."
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE photos (
    id               UUID         NOT NULL DEFAULT uuid_generate_v4(),
    user_id          UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            VARCHAR(500) NOT NULL,
    product_id       VARCHAR(100),              -- nullable: not all photos are product-linked
    filename         VARCHAR(255) NOT NULL,
    file_path        TEXT         NOT NULL,     -- MinIO object key: e.g. "2026/03/abc123.jpg"
    size_bytes       BIGINT       NOT NULL,
    content_type     VARCHAR(100) NOT NULL DEFAULT 'image/jpeg',
    -- CONCEPT: Denormalizing storage_tier into the metadata table.
    -- Lets the migration cron and download service know which MinIO bucket to use
    -- without extra lookups. Eventual consistency with MinIO is fine (NFR #3).
    storage_tier     VARCHAR(10)  NOT NULL DEFAULT 'HOT'
                     CHECK (storage_tier IN ('HOT', 'WARM', 'COLD')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- last_accessed_at drives storage tier migration decisions.
    -- Updated on every view/download via async background write (not on the hot path).
    last_accessed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
) PARTITION BY RANGE (created_at);

-- ── Create one partition per year (2024–2030) ─────────────────────────────────
-- In production: use pg_partman extension for automatic partition management.
CREATE TABLE photos_2024 PARTITION OF photos FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE photos_2025 PARTITION OF photos FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE photos_2026 PARTITION OF photos FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE photos_2027 PARTITION OF photos FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TABLE photos_2028 PARTITION OF photos FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');
CREATE TABLE photos_2029 PARTITION OF photos FOR VALUES FROM ('2029-01-01') TO ('2030-01-01');
CREATE TABLE photos_2030 PARTITION OF photos FOR VALUES FROM ('2030-01-01') TO ('2031-01-01');
-- Catch-all: prevents insert failures for unexpected dates
CREATE TABLE photos_default PARTITION OF photos DEFAULT;

-- ── Indexes ───────────────────────────────────────────────────────────────────
-- CONCEPT: Each index answers exactly ONE query pattern.
--          Over-indexing hurts write throughput (23 photos/sec × N indexes).
--          Under-indexing causes full-partition scans at 5B rows.

-- Primary: Fetch photo by ID.
-- Must include created_at (partition key) for PostgreSQL to do partition pruning.
CREATE UNIQUE INDEX idx_photos_id          ON photos (id, created_at);

-- User gallery: "Show me all photos uploaded by user X, newest first."
-- Composite (user_id, created_at DESC) avoids a sort step.
CREATE INDEX idx_photos_user_created       ON photos (user_id, created_at DESC);

-- Product lookup: "Show all photos tagged with product_id = 'P001'."
-- CONCEPT: PARTIAL INDEX — only indexes rows where product_id IS NOT NULL.
-- ~30% of photos may have no product_id. Partial index is ~30% smaller,
-- faster to build, and consumes less memory during query.
CREATE INDEX idx_photos_product_id         ON photos (product_id, created_at DESC)
    WHERE product_id IS NOT NULL;

-- Storage tier migration: "Find HOT photos not accessed in 1+ year."
-- Used exclusively by the migration cron job.
CREATE INDEX idx_photos_tier_accessed      ON photos (storage_tier, last_accessed_at);

-- ── Photo access log: For analytics and LRU-based tier decisions ─────────────
-- CONCEPT: Write-ahead / append-only log pattern.
-- Rather than UPDATE photos SET last_accessed_at = NOW() on every view
-- (causes hot-row contention at 2,300 reads/sec), we INSERT a log record
-- and a batch job aggregates last_accessed_at periodically.
CREATE TABLE photo_access_log (
    id          BIGSERIAL    PRIMARY KEY,
    photo_id    UUID         NOT NULL,
    accessed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source      VARCHAR(20)  NOT NULL DEFAULT 'api'
                CHECK (source IN ('api', 'cache_miss', 'search', 'download'))
) PARTITION BY RANGE (accessed_at);

CREATE TABLE photo_access_log_2024 PARTITION OF photo_access_log FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE photo_access_log_2025 PARTITION OF photo_access_log FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE photo_access_log_2026 PARTITION OF photo_access_log FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE photo_access_log_2027 PARTITION OF photo_access_log FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TABLE photo_access_log_2028 PARTITION OF photo_access_log FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');
CREATE TABLE photo_access_log_2029 PARTITION OF photo_access_log FOR VALUES FROM ('2029-01-01') TO ('2030-01-01');
CREATE TABLE photo_access_log_2030 PARTITION OF photo_access_log FOR VALUES FROM ('2030-01-01') TO ('2031-01-01');
CREATE TABLE photo_access_log_default PARTITION OF photo_access_log DEFAULT;

CREATE INDEX idx_access_log_photo_time ON photo_access_log (photo_id, accessed_at DESC);

-- ── Demo user ─────────────────────────────────────────────────────────────────
INSERT INTO users (id, username, email) VALUES
    ('00000000-0000-0000-0000-000000000001', 'demo_user', 'demo@photosystem.local');
