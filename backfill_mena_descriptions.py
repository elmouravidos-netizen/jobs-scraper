"""
backfill_mena_descriptions.py

PURPOSE
───────
Fixes the root cause behind the GSC "Discovered - currently not indexed" spike
(19,828 pages stuck, never crawled). LinkedIn and Wuzzuf jobs currently get
saved with description_en = "Full details at {url}" — zero real content — and
description_ar is never filled at all. This script:

  1. Finds jobs still stuck with the placeholder description
  2. Visits the real source_url and pulls the actual posting text
  3. Cleans it (strips nav/footer/ad junk) and asks the AI to turn it into a
     proper ~100-150 word professional Arabic description
  4. Updates description_en (real snippet) and description_ar (clean Arabic)
  5. Is fully resumable — safe to run repeatedly, processes a capped batch
     per run so it fits inside a GitHub Actions timeout

SAFETY
──────
- Does NOT touch scraper.py or scraper_shared.py — purely additive.
- Only ever UPDATEs existing rows by job_key — never inserts, never deletes.
- Per-job try/except — one bad page never kills the whole run.
- BATCH_LIMIT caps how many jobs this run touches, so it's safe to schedule
  on its own timeout and let it catch up gradually across multiple runs.
"""

import os
import re
import json
import asyncio
import logging
import urllib.request
from datetime import datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ──────────────────────────────────────────────────────────────
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

TRANSLATE_MODEL = "qwen/qwen-2.5-72b-instruct"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Tunables ─────────────────────────────────────────────────────────────────
BATCH_LIMIT      = 150   # jobs processed per run — keeps this well inside a 30-45min timeout
AI_BATCH_SIZE    = 8     # jobs per OpenRouter call — descriptions are longer than titles, keep batches smaller
PLACEHOLDER_MARK = "Full details at"   # how we identify jobs still needing a real description
RAW_TEXT_CAP     = 2500  # chars of raw scraped page text sent to the AI per job

# Phrases that indicate we've hit junk/nav/footer content — cut everything from here on
JUNK_CUTOFFS = [
    "report this ad", "similar jobs", "related jobs", "وظائف مشابهة",
    "sign in", "create an account", "cookie policy", "privacy policy",
    "all rights reserved", "apply now", "share this job",
]


# ══════════════════════════════════════════════════════════════════════════════
#  TEXT CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

def clean_raw_text(text: str) -> str:
    """Collapse whitespace and cut off at the first junk marker found."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    lower = text.lower()
    cut_at = len(text)
    for phrase in JUNK_CUTOFFS:
        idx = lower.find(phrase)
        if idx != -1:
            cut_at = min(cut_at, idx)
    return text[:cut_at].strip()[:RAW_TEXT_CAP]


# ══════════════════════════════════════════════════════════════════════════════
#  DB — fetch jobs still needing a real description
# ══════════════════════════════════════════════════════════════════════════════

def fetch_jobs_needing_description(limit: int) -> list[dict]:
    try:
        result = (
            supabase.table("jobs")
            .select("job_key, title_en, title_ar, source_url, source_platform")
            .ilike("description_en", f"{PLACEHOLDER_MARK}%")
            .in_("source_platform", ["LinkedIn", "Wuzzuf"])
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        log.error(f"❌ Failed to fetch jobs needing description: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPE — visit the real posting and grab the raw text
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_description(page, url: str, platform: str) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)

        candidates = []
        if platform == "LinkedIn":
            candidates = [
                "div.show-more-less-html__markup",
                "div.description__text",
            ]
        elif platform == "Wuzzuf":
            candidates = [
                "div.css-1lh32fc",   # common Wuzzuf description container (may drift over time)
                "section.job-description",
            ]

        for sel in candidates:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = await el.inner_text()
                    cleaned = clean_raw_text(text)
                    if len(cleaned) > 80:   # sanity check — too short means we grabbed the wrong element
                        return cleaned
            except Exception:
                continue

        # Fallback: whole-page text, cleaned and cut at junk markers
        body_text = await page.locator("body").inner_text()
        return clean_raw_text(body_text)

    except PlaywrightTimeout:
        log.warning(f"   ⚠ Timeout loading {url}")
        return ""
    except Exception as e:
        log.warning(f"   ⚠ Scrape error {url}: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
#  AI — turn raw noisy text into a clean, professional Arabic description
# ══════════════════════════════════════════════════════════════════════════════

def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


async def ai_clean_and_translate_batch(jobs_batch: list[dict]) -> list[str]:
    """
    Takes jobs with a `raw_text` field (already scraped) and returns a list of
    clean Arabic descriptions (~100-150 words each), same order as input.
    Falls back to empty string per-job on failure — never crashes the batch.
    """
    entries = []
    for i, j in enumerate(jobs_batch):
        raw = j["raw_text"] if j["raw_text"] else "(no content available)"
        entries.append(f"### Job {i+1}\nTitle: {j['title_en']}\nRaw text: {raw}")

    prompt = (
        "You are a professional Arabic HR content writer. For each numbered job below, "
        "write a clean, professional Arabic job description of 80-120 words. "
        "If the 'Raw text' contains real job details, summarize them professionally. "
        "CRITICAL: If the 'Raw text' is empty, says '(no content available)', or contains no usable info, "
        "you MUST invent a highly professional, realistic, and generic Arabic job description "
        "based SOLELY on the 'Title'. Do not return an empty string. Do not say 'no info available'.\n\n"
        "Return ONLY a valid JSON
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2200,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer":  "https://github.com/mena-jobs-scraper",
        "X-Title":       "MENA Jobs Description Backfill",
    }

    for attempt in range(1, 4):
        try:
            resp = http_post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
            raw_content = resp["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if the model added them despite instructions
            raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content.strip())
            parsed = json.loads(raw_content)
            if isinstance(parsed, list) and len(parsed) == len(jobs_batch):
                return [str(x).strip() for x in parsed]
            log.warning(f"   AI batch returned wrong shape (attempt {attempt}), retrying")
        except Exception as e:
            log.warning(f"   AI batch attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 ** attempt)

    log.error("   ❌ AI batch failed all attempts — leaving these jobs for next run")
    return [""] * len(jobs_batch)


# ══════════════════════════════════════════════════════════════════════════════
#  DB — save results
# ══════════════════════════════════════════════════════════════════════════════

def save_description(job_key: str, description_en: str, description_ar: str) -> bool:
    try:
        update = {
            "description_ar": description_ar,
            "translation_status": "completed",
        }
        if description_en:
            update["description_en"] = description_en
        supabase.table("jobs").update(update).eq("job_key", job_key).execute()
        return True
    except Exception as e:
        log.error(f"   ❌ DB update failed for {job_key}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA description backfill — starting")
    start = datetime.now()

    jobs = fetch_jobs_needing_description(BATCH_LIMIT)
    log.info(f"📦 {len(jobs)} jobs need real descriptions this run (capped at {BATCH_LIMIT})")

    if not jobs:
        log.info("✅ Nothing to do — all caught up.")
        return

    # ── Phase 1: scrape real text for each job ─────────────────────────────
    log.info("\n── Phase 1: Scraping source pages ──")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        for j in jobs:
            log.info(f"🌐 [{j['source_platform']}] {j['title_en'][:50]}")
            j["raw_text"] = await scrape_description(page, j["source_url"], j["source_platform"])
            await asyncio.sleep(0.5)  # be polite to source sites
        await browser.close()

    scraped_ok = sum(1 for j in jobs if j["raw_text"])
    log.info(f"   ✅ Got real content for {scraped_ok}/{len(jobs)} jobs")

    # ── Phase 2: AI clean + translate in batches ────────────────────────────
    async def ai_clean_and_translate_batch(jobs_batch: list[dict]) -> list[str]:
    """
    Takes jobs with a `raw_text` field (already scraped) and returns a list of
    clean Arabic descriptions (~100-150 words each), same order as input.
    Falls back to empty string per-job on failure — never crashes the batch.
    """
    entries = []
    for i, j in enumerate(jobs_batch):
        raw = j["raw_text"] if j["raw_text"] else "(no content available)"
        entries.append(f"### Job {i+1}\nTitle: {j['title_en']}\nRaw text: {raw}")

    jobs_list = "\n\n".join(entries)
    
    prompt = (
        "You are a professional Arabic HR content writer. For each numbered job below, "
        "write a clean, professional Arabic job description of 100-150 words based on the "
        "raw scraped text. Ignore any navigation menus, ads, or unrelated site content in "
        "the raw text — extract only genuine job information (responsibilities, requirements, "
        "what the role involves). If the raw text has no usable job information, write a "
        "reasonable general Arabic description based on the job title alone.\n\n"
        "Return ONLY a JSON array of strings, one per job, in the same order. No explanations, "
        "no markdown, no extra text — just the raw JSON array.\n\n"
        + jobs_list
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2200,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer":  "https://github.com/mena-jobs-scraper",
        "X-Title":       "MENA Jobs Description Backfill",
    }
    for attempt in range(1, 4):
        try:
            resp = http_post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
            raw_content = resp["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if the model added them despite instructions
            raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content.strip())
            parsed = json.loads(raw_content)
            if isinstance(parsed, list) and len(parsed) == len(jobs_batch):
                return [str(x).strip() for x in parsed]
            log.warning(f"   AI batch returned wrong shape (attempt {attempt}), retrying")
        except Exception as e:
            log.warning(f"   AI batch attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 ** attempt)
    log.error("   ❌ AI batch failed all attempts — leaving these jobs for next run")
    return [""] * len(jobs_batch)

if __name__ == "__main__":
    asyncio.run(main())
