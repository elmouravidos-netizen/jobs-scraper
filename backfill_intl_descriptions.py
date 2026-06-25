"""
INTERNATIONAL JOBS DESCRIPTION BACKFILL
-----------------------------------------
Re-fetches clean descriptions for international jobs where
description_en is empty or was polluted with "Report this ad" content.

Also re-translates description to Arabic for completed jobs.

Run once after cleaning polluted descriptions in Supabase.

Usage:
  python backfill_intl_descriptions.py
"""

import os
import re
import asyncio
import logging
import time
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

# Import shared translation utility
from scraper_shared import translate_title_and_description

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

TRANSLATE_ENABLED  = bool(OPENROUTER_API_KEY)
log.info(f"{'✅' if TRANSLATE_ENABLED else '⏭ '} Translation {'ENABLED' if TRANSLATE_ENABLED else 'SKIPPED'}")

supabase: Client   = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ─────────────────────────────────────────────────────────────────────
FETCH_TIMEOUT      = 20    # seconds per page
BATCH_SIZE         = 20    # jobs processed per browser session
PAUSE_BETWEEN      = 0.8   # seconds between jobs

# Phrases that indicate we scraped UI noise instead of real description
CUTOFF_PHRASES = [
    "Report this ad",
    "Report this job",
    "الإبلاغ عن الإعلان",
    "سبب الإبلاغ",
    "Flag this job",
    "احتيالي",
    "رابط معطوب",
    "Reason for",
    "Is this job ad",
]


def clean_description(text: str) -> str:
    """Cut before report form content, clean whitespace."""
    if not text:
        return ""
    for phrase in CUTOFF_PHRASES:
        if phrase in text:
            text = text[:text.index(phrase)].strip()
    # Clean excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()[:2000]


async def fetch_description(page, url: str) -> str:
    """Fetch clean description from a job detail page."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=FETCH_TIMEOUT * 1000)
        await page.wait_for_timeout(1500)

        for sel in [
            "[class*='job-description']",
            "[class*='description']",
            "[class*='job-detail']",
            "[class*='job_detail']",
            "[class*='content']",
            "article",
            "main",
        ]:
            try:
                text = await page.locator(sel).first.inner_text(timeout=3000)
                cleaned = clean_description(text)
                if cleaned and len(cleaned) > 100:
                    return cleaned
            except Exception:
                continue

        return ""
    except (PlaywrightTimeout, Exception) as e:
        log.debug(f"   Fetch error: {e}")
        return ""


async def process_batch(jobs: list[dict], ctx) -> tuple[int, int, int]:
    """Process one batch of jobs — fetch descriptions and translate."""
    fetched = translated = failed = 0

    for job in jobs:
        log.info(f"\n  📄 [{job['country']}] {job['title_en'][:50]}")

        page = await ctx.new_page()
        try:
            desc = await fetch_description(page, job["source_url"])
        finally:
            await page.close()

        if not desc:
            log.warning(f"  ⚠  No description found — skipping")
            failed += 1
            continue

        log.info(f"  ✅ Got description ({len(desc)} chars)")
        fetched += 1

        # Translate if OpenRouter available
        title_ar = job.get("title_ar", "")
        desc_ar  = ""

        if TRANSLATE_ENABLED:
            log.info(f"  🤖 Translating description...")
            _, desc_ar = await translate_title_and_description(
                job["title_en"], desc, OPENROUTER_API_KEY
            )
            if desc_ar:
                log.info(f"  ✅ Translated ({len(desc_ar)} chars)")
                translated += 1

        # Update Supabase
        update_data = {
            "description_en":     desc,
            "description_ar":     desc_ar,
            "translation_status": "completed" if title_ar and desc_ar else "pending",
        }

        try:
            supabase.table("jobs").update(update_data).eq("id", job["id"]).execute()
            log.info(f"  💾 Saved to DB")
        except Exception as e:
            log.error(f"  ❌ DB update error: {e}")
            failed += 1

        await asyncio.sleep(PAUSE_BETWEEN)

    return fetched, translated, failed


async def main():
    log.info("🚀 International Jobs Description Backfill — starting")
    start_time = time.time()

    # ── Fetch all international jobs with empty descriptions ──────────────────
    log.info("\n── Fetching jobs needing descriptions ──")
    result = (
        supabase.table("jobs")
        .select("id, title_en, title_ar, source_url, country, job_category")
        .eq("job_type", "international_contract")
        .eq("is_active", True)
        .or_("description_en.eq.,description_en.is.null")
        .execute()
    )

    jobs = result.data or []
    log.info(f"📦 Found {len(jobs)} jobs needing descriptions")

    if not jobs:
        log.info("✅ Nothing to backfill — all international jobs have descriptions!")
        return

    # ── Process in batches to avoid browser memory issues ────────────────────
    total_fetched = total_translated = total_failed = 0

    for batch_start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(jobs) + BATCH_SIZE - 1) // BATCH_SIZE

        log.info(f"\n{'='*55}")
        log.info(f"📦 Batch {batch_num}/{total_batches} — {len(batch)} jobs")
        log.info(f"{'='*55}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )

            fetched, translated, failed = await process_batch(batch, ctx)
            await browser.close()

        total_fetched     += fetched
        total_translated  += translated
        total_failed      += failed

        # Progress report after each batch
        done = batch_start + len(batch)
        pct  = done / len(jobs) * 100
        elapsed = int(time.time() - start_time)
        log.info(f"\n📊 Progress: {done}/{len(jobs)} ({pct:.0f}%) in {elapsed}s")
        log.info(f"   ✅ Fetched: {total_fetched} | 🤖 Translated: {total_translated} | ❌ Failed: {total_failed}")

        # Small pause between batches
        if batch_start + BATCH_SIZE < len(jobs):
            log.info("   ⏳ Pausing 3s before next batch...")
            await asyncio.sleep(3)

    elapsed = int(time.time() - start_time)
    log.info(f"\n{'='*55}")
    log.info(f"🏁 Backfill complete in {elapsed}s")
    log.info(f"   ✅ Descriptions fetched: {total_fetched}")
    log.info(f"   🤖 Descriptions translated: {total_translated}")
    log.info(f"   ❌ Failed: {total_failed}")
    log.info(f"   📦 Total processed: {total_fetched + total_failed}/{len(jobs)}")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    asyncio.run(main())
