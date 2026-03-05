"""
Standalone tier migration cron job.
Runs as a separate Docker service (migration-worker) on a schedule.

SYSTEM DESIGN CONCEPT: Separation of Concerns
  The migration job is completely decoupled from the API tier.
  Benefits:
  - Can be scaled, restarted, or paused without affecting the API
  - Different resource requirements (CPU/memory) from the API
  - Failure of migration worker doesn't impact photo upload/view
  - Can be run as: AWS Lambda on EventBridge, Kubernetes CronJob, or Celery Beat

RUN MANUALLY:
  docker-compose exec backend python /app/scripts/tier_migration_cron.py
"""
import asyncio
import logging
import sys
import os

# Add backend root to path when run as standalone script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.tier_migration import TierMigrationService
from config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tier_migration_cron")

settings = get_settings()


async def run_once():
    logger.info("=" * 60)
    logger.info("Tier Migration Job Starting")
    logger.info(f"  HOT → WARM threshold: {settings.hot_to_warm_days} days")
    logger.info(f"  WARM → COLD threshold: {settings.warm_to_cold_days} days")
    logger.info("=" * 60)

    service = TierMigrationService()
    stats = await service.run_migration()

    logger.info("Migration Summary:")
    logger.info(f"  HOT  → WARM: {stats['hot_to_warm']} photos moved")
    logger.info(f"  WARM → COLD: {stats['warm_to_cold']} photos moved")
    logger.info(f"  Errors:      {stats['errors']}")
    logger.info("=" * 60)
    return stats


async def run_with_interval():
    """
    Run migration on a schedule (interval = MIGRATION_INTERVAL_SECONDS).
    In production: replace with Kubernetes CronJob or AWS EventBridge rule.
    """
    interval = int(os.getenv("MIGRATION_INTERVAL_SECONDS", "86400"))  # default: 24h
    logger.info(f"Migration worker started. Interval: {interval}s ({interval/3600:.1f}h)")

    while True:
        try:
            await run_once()
        except Exception as e:
            logger.error(f"Migration run failed: {e}", exc_info=True)

        logger.info(f"Sleeping for {interval}s until next run...")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    # Run as scheduled worker
    asyncio.run(run_with_interval())
