"""
SYSTEM DESIGN CONCEPT: 12-Factor App Configuration

All config comes from environment variables — never hardcoded in source.
Benefits:
  - Same Docker image deployed to dev/staging/prod with different .env files
  - Secrets (DB passwords, API keys) never committed to git
  - Easy to override in Kubernetes via ConfigMaps/Secrets
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://photouser:photopass@localhost:5432/photodb"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Elasticsearch
    es_url: str = "http://localhost:9200"

    # MinIO / S3-compatible object storage
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_secure: bool = False

    # Storage tier bucket names
    # HOT  → S3 Standard    (0-12 months)
    # WARM → S3 Standard-IA (1-3 years)  ~40% cheaper
    # COLD → S3 Glacier     (3-7 years)  ~80% cheaper, 3-5hr retrieval
    hot_bucket: str = "photos-hot"
    warm_bucket: str = "photos-warm"
    cold_bucket: str = "photos-cold"

    # Tier migration thresholds (days since last_accessed_at)
    hot_to_warm_days: int = 365
    warm_to_cold_days: int = 1095  # 3 years

    # Cache
    cache_ttl: int = 3600  # seconds

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    def bucket_for_tier(self, tier: str) -> str:
        return {
            "HOT": self.hot_bucket,
            "WARM": self.warm_bucket,
            "COLD": self.cold_bucket,
        }[tier]


@lru_cache
def get_settings() -> Settings:
    return Settings()
