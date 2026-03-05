"""
SYSTEM DESIGN CONCEPT: Storage Tiering & Data Lifecycle Management

COST MATH (why tiering matters at scale):
  Total storage: ~1.02 PB over 7 years
  If all HOT (S3 Standard):  1,020 TB × $0.023/GB = $23,460/month
  With tiering:
    HOT  (yr 1):     146 TB × $0.023  =  $3,358/month
    WARM (yr 1-3):   292 TB × $0.0125 =  $3,650/month
    COLD (yr 3-7):   584 TB × $0.004  =  $2,336/month
    Total:                              ~$9,344/month
  Saving: ~60% ($14,116/month, $169K/year at scale)

MIGRATION LOGIC:
  HOT  → WARM:  last_accessed_at < NOW() - 365 days  AND tier = 'HOT'
  WARM → COLD:  last_accessed_at < NOW() - 1095 days AND tier = 'WARM'

ACCESS PATTERN INSIGHT:
  Photos follow a power law: ~5% of photos get 95% of views.
  Old, unaccessed photos accumulate in HOT tier wasting money.
  Migration based on last_accessed_at ensures popular old photos stay warm.

COPY-THEN-DELETE SAFETY:
  Object storage has no atomic move. Strategy:
  1. Copy to destination tier
  2. Verify destination object exists (stat check)
  3. Update DB tier field to new tier
  4. Delete from source tier
  If step 3 fails: object exists in both tiers (wasted space, not data loss).
  On next run, the DB still shows old tier, so migration retries safely.
"""
import asyncio
import uuid
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from db.connection import AsyncSessionLocal
from models.db_models import Photo
from services.storage_service import StorageService
from config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class TierMigrationService:

    def __init__(self):
        self.storage = StorageService()

    async def run_migration(self) -> dict:
        """
        Main migration loop. Processes all eligible photos in batches.
        Returns a summary of actions taken.
        """
        now = datetime.now(timezone.utc)
        hot_cutoff  = now - timedelta(days=settings.hot_to_warm_days)
        warm_cutoff = now - timedelta(days=settings.warm_to_cold_days)

        stats = {"hot_to_warm": 0, "warm_to_cold": 0, "errors": 0}

        async with AsyncSessionLocal() as session:
            # Migrate HOT → WARM
            moved = await self._migrate_batch(session, "HOT", "WARM", hot_cutoff)
            stats["hot_to_warm"] += moved

            # Migrate WARM → COLD
            moved = await self._migrate_batch(session, "WARM", "COLD", warm_cutoff)
            stats["warm_to_cold"] += moved

        logger.info(f"Tier migration complete: {stats}")
        return stats

    async def _migrate_batch(self, session: AsyncSession, from_tier: str,
                              to_tier: str, cutoff: datetime, batch_size: int = 100) -> int:
        """
        Process photos eligible for tier migration in batches.
        Batching prevents memory exhaustion and keeps DB transactions short.
        CONCEPT: Batch processing vs. stream processing trade-off.
        """
        total_moved = 0
        offset = 0

        while True:
            # Find candidates: tier matches AND not accessed since cutoff
            stmt = (
                select(Photo.id, Photo.file_path, Photo.storage_tier)
                .where(Photo.storage_tier == from_tier)
                .where(Photo.last_accessed_at < cutoff)
                .limit(batch_size)
                .offset(offset)
            )
            result = await session.execute(stmt)
            candidates = result.fetchall()

            if not candidates:
                break

            for photo_id, file_path, tier in candidates:
                try:
                    # 1. Move object in MinIO
                    self.storage.move_to_tier(file_path, from_tier, to_tier)

                    # 2. Update DB metadata (source of truth)
                    await session.execute(
                        update(Photo)
                        .where(Photo.id == photo_id)
                        .values(storage_tier=to_tier)
                    )
                    await session.commit()
                    total_moved += 1
                    logger.debug(f"Moved photo {photo_id}: {from_tier} → {to_tier}")

                except Exception as e:
                    await session.rollback()
                    logger.error(f"Failed to migrate photo {photo_id}: {e}")
                    # Continue with next photo — don't let one failure block the batch

            offset += batch_size

        return total_moved
