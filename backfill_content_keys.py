"""
backfill_content_keys.py
─────────────────────────
ONE-TIME script. Run this AFTER the SQL migration (adding the
content_key column) and AFTER deploying the updated scraper.py.

What it does:
1. Pulls every existing job row (title_en, company_name, country)
2. Computes the same content_key fingerprint scraper.py now uses
3. Writes it back to each row
4. Reports how many duplicate groups exist among EXISTING jobs, so
   you know the scale of the historical cleanup needed

This does NOT delete or deactivate anything — it only populates the
column so the unique index / future dedup logic has something to
work with. Deciding which duplicate in each group to keep (usually:
newest posted_at, or the one already indexed by Google) is a
separate manual/semi-automated step — run this first to see the
numbers before deciding.
"""
import os
import re
import hashlib
import logging
from collections import defaultdict
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BATCH_SIZE = 500


def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    noise = {
        'urgent', 'immediate', 'hiring', 'new', 'job', 'jobs', 'vacancy',
        'vacancies', 'opportunity', 'position', 'full', 'time', 'part',
    }
    tokens = [w for w in t.split() if w not in noise]
    return ' '.join(tokens)


def make_content_key(title: str, company: str, country: str) -> str:
    fingerprint = f"{normalize_text(title)}::{normalize_text(company)}::{(country or '').upper()}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def fetch_all_jobs() -> list[dict]:
    all_rows = []
    page = 0
    while True:
        result = (
            supabase.table("jobs")
            .select("id, title_en, company_name, country, posted_at, content_key")
            .range(page * 1000, (page + 1) * 1000 - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return all_rows


def main():
    log.info("📥 Fetching all jobs...")
    jobs = fetch_all_jobs()
    log.info(f"   {len(jobs)} total rows")

    # Compute content_key for every row and group by it to find duplicates
    groups = defaultdict(list)
    updates = []
    for j in jobs:
        ck = make_content_key(j.get("title_en", ""), j.get("company_name", ""), j.get("country", ""))
        groups[ck].append(j)
        if j.get("content_key") != ck:
            updates.append({"id": j["id"], "content_key": ck})

    dup_groups = {ck: rows for ck, rows in groups.items() if len(rows) > 1}
    total_dupes = sum(len(rows) - 1 for rows in dup_groups.values())  # extras beyond the one to keep

    log.info(f"\n📊 DUPLICATE ANALYSIS")
    log.info(f"   Unique jobs (by content):  {len(groups)}")
    log.info(f"   Duplicate groups found:    {len(dup_groups)}")
    log.info(f"   Extra duplicate rows:      {total_dupes}")
    log.info(f"   (i.e. {total_dupes} rows could be deactivated once you decide which to keep per group)")

    log.info(f"\n💾 Writing content_key to {len(updates)} rows that need it...")
    saved = 0
    for i in range(0, len(updates), BATCH_SIZE):
        batch = updates[i:i + BATCH_SIZE]
        for row in batch:
            try:
                supabase.table("jobs").update({"content_key": row["content_key"]}).eq("id", row["id"]).execute()
                saved += 1
            except Exception as e:
                log.error(f"   ❌ Update failed for id={row['id']}: {e}")
        log.info(f"   ...{min(i + BATCH_SIZE, len(updates))}/{len(updates)} done")

    log.info(f"\n🏁 Done — {saved}/{len(updates)} rows updated with content_key")
    log.info("   Next step: review dup_groups (largest ones first) and decide which")
    log.info("   row per group to keep active — typically the one with the most")
    log.info("   complete data (has description_ar) or the earliest posted_at.")


if __name__ == "__main__":
    main()
