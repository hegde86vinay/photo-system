"""
Interactive Capacity Calculator for System Design Interviews

Run this script to understand the numbers behind the photo system.
Use these numbers confidently in any system design interview.

Usage:
  python capacity_calc.py
  docker-compose exec backend python /app/scripts/capacity_calc.py
"""


def hr():
    print("─" * 65)


def section(title: str):
    print(f"\n{'═' * 65}")
    print(f"  {title}")
    print(f"{'═' * 65}")


def main():
    print("\n" + "=" * 65)
    print("   PHOTO SYSTEM — CAPACITY ESTIMATION (System Design Interview)")
    print("=" * 65)

    # ─────────────────────────────────────────────────────────────────
    section("1. GIVEN ASSUMPTIONS")
    # ─────────────────────────────────────────────────────────────────
    total_users        = 500_000_000
    dau                = 1_000_000
    photos_per_day     = 2_000_000
    avg_photo_kb       = 200
    retention_years    = 7
    read_write_ratio   = 100       # industry avg for photo apps

    print(f"  Total users:         {total_users:>15,}")
    print(f"  Daily active users:  {dau:>15,}")
    print(f"  New photos/day:      {photos_per_day:>15,}")
    print(f"  Avg photo size:      {avg_photo_kb:>15} KB")
    print(f"  Retention:           {retention_years:>15} years")
    print(f"  Read:write ratio:    {read_write_ratio:>14}:1")

    # ─────────────────────────────────────────────────────────────────
    section("2. WRITE QPS")
    # ─────────────────────────────────────────────────────────────────
    write_per_sec = photos_per_day / 86_400
    peak_write    = write_per_sec * 3    # assume 3x peak vs avg
    read_per_sec  = write_per_sec * read_write_ratio
    peak_read     = read_per_sec  * 3

    print(f"  Avg writes/sec:      {write_per_sec:>15.1f}")
    print(f"  Peak writes/sec:     {peak_write:>15.1f}  (3× avg)")
    print(f"  Avg reads/sec:       {read_per_sec:>15.1f}  ({read_write_ratio}× writes)")
    print(f"  Peak reads/sec:      {peak_read:>15.1f}  (3× avg)")
    print()
    print("  INTERVIEW TIP: At 2,300 reads/sec, a single PostgreSQL instance")
    print("  handles this easily (can do ~10K QPS for simple selects).")
    print("  Redis cache at 80% hit ratio drops DB load to ~460 reads/sec.")

    # ─────────────────────────────────────────────────────────────────
    section("3. STORAGE ESTIMATION")
    # ─────────────────────────────────────────────────────────────────
    bytes_per_day   = photos_per_day * avg_photo_kb * 1024
    gb_per_day      = bytes_per_day / 1e9
    tb_per_year     = gb_per_day * 365 / 1000
    pb_total        = tb_per_year * retention_years / 1000
    metadata_bytes  = photos_per_day * retention_years * 365 * 500  # ~500B per row
    metadata_tb     = metadata_bytes / 1e12

    print(f"  Storage per day:     {gb_per_day:>14.1f} GB")
    print(f"  Storage per year:    {tb_per_year:>14.1f} TB")
    print(f"  Total (7 years):     {pb_total:>14.2f} PB  ← Use this number!")
    print()
    print(f"  Metadata (DB rows):  ~{metadata_tb:.1f} TB  (5.1B rows, ~500B each)")
    print()
    print("  INTERVIEW TIP: ~1 PB of photo data. Always follow with tiering:")
    print("  HOT (yr 1): 146TB, WARM (yr 1-3): 292TB, COLD (yr 3-7): 584TB")

    # ─────────────────────────────────────────────────────────────────
    section("4. BANDWIDTH ESTIMATION")
    # ─────────────────────────────────────────────────────────────────
    upload_bw_mbs   = write_per_sec * avg_photo_kb / 1000
    download_bw_mbs = read_per_sec  * avg_photo_kb / 1000
    peak_dl_gbps    = download_bw_mbs / 1000 * 8  # Gbps

    print(f"  Upload bandwidth:    {upload_bw_mbs:>14.1f} MB/s avg")
    print(f"  Download bandwidth:  {download_bw_mbs:>14.1f} MB/s avg")
    print(f"  Peak download:       {peak_dl_gbps:>14.2f} Gbps")
    print()
    print("  INTERVIEW TIP: ~3.68 Gbps peak → CDN is MANDATORY.")
    print("  CDN absorbs 95%+ of read traffic. Backend only sees cache misses.")

    # ─────────────────────────────────────────────────────────────────
    section("5. STORAGE COST WITH TIERING (per month)")
    # ─────────────────────────────────────────────────────────────────
    hot_tb  = tb_per_year          # year 1
    warm_tb = tb_per_year * 2      # year 1-3
    cold_tb = tb_per_year * 4      # year 3-7

    # AWS S3 pricing (approximate)
    cost_hot  = hot_tb  * 1000 * 0.023   # Standard:    $0.023/GB
    cost_warm = warm_tb * 1000 * 0.0125  # Standard-IA: $0.0125/GB
    cost_cold = cold_tb * 1000 * 0.004   # Glacier:     $0.004/GB
    cost_all_hot = (hot_tb + warm_tb + cold_tb) * 1000 * 0.023

    print(f"  HOT  ({hot_tb:.0f} TB, S3 Standard):     ${cost_hot:>8,.0f}/month")
    print(f"  WARM ({warm_tb:.0f} TB, S3 Standard-IA): ${cost_warm:>8,.0f}/month")
    print(f"  COLD ({cold_tb:.0f} TB, S3 Glacier):      ${cost_cold:>8,.0f}/month")
    hr()
    print(f"  Total with tiering:              ${cost_hot+cost_warm+cost_cold:>8,.0f}/month")
    print(f"  Total without tiering (all HOT): ${cost_all_hot:>8,.0f}/month")
    print(f"  Annual savings:                  ${(cost_all_hot - cost_hot - cost_warm - cost_cold)*12:>8,.0f}/year")

    # ─────────────────────────────────────────────────────────────────
    section("6. CACHE SIZING")
    # ─────────────────────────────────────────────────────────────────
    hot_photos_pct  = 0.20  # 20% of photos get 80% of reads (Pareto)
    daily_unique    = dau * 5  # assume 5 unique photos viewed per DAU
    hot_photo_count = daily_unique * hot_photos_pct
    cache_size_mb   = hot_photo_count * 1  # ~1KB per metadata entry

    print(f"  DAU × 5 photos each: {daily_unique:>15,} views/day")
    print(f"  Top 20% of photos:   {hot_photo_count:>15,.0f} unique hot photos")
    print(f"  Cache size needed:   {cache_size_mb:>15,.0f} KB = {cache_size_mb/1024:.1f} MB")
    print()
    print("  INTERVIEW TIP: Only ~5-50 MB for hot metadata cache.")
    print("  Redis 256MB config in this system has massive headroom.")

    # ─────────────────────────────────────────────────────────────────
    section("7. DATABASE ROW COUNT & SHARDING THRESHOLD")
    # ─────────────────────────────────────────────────────────────────
    total_rows      = photos_per_day * 365 * retention_years
    rows_per_shard  = 200_000_000   # comfortable PostgreSQL shard size

    print(f"  Total rows (7 years): {total_rows:>14,}")
    print(f"  Rows/shard target:    {rows_per_shard:>14,}")
    print(f"  Shards needed:        {total_rows / rows_per_shard:>14.0f}")
    print()
    print("  INTERVIEW TIP: We use table partitioning (7 year-partitions) first.")
    print("  True horizontal sharding (separate DB instances) if > 10B rows.")
    print("  Shard key: user_id hash for balanced writes, or created_at for archival.")

    print("\n" + "=" * 65)
    print("  Run `docker-compose up` to start the system and see it live!")
    print("  API docs: http://localhost:8000/docs")
    print("  MinIO UI: http://localhost:9001  (minioadmin / minioadmin123)")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
