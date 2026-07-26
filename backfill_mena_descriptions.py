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
     proper ~120-180 word professional Arabic description
  4. Updates description_en (real snippet), description_ar, and title_ar
     (if it was still missing)
  5. Is fully resumable — safe to run repeatedly, processes a capped batch
     per run so it fits inside a GitHub Actions timeout

AI STRATEGY
───────────
- PRIMARY: Google Gemini (FREE, fast, excellent Arabic)
- FALLBACK: OpenRouter Qwen 2.5 72B (only if Gemini fails/unconfigured)
- Gemini calls are throttled to stay under the free-tier 15 req/min limit

SAFETY
──────
- Does NOT touch scraper.py or scraper_shared.py — purely additive.
- Only ever UPDATEs existing rows by job_key — never inserts, never deletes.
- Per-job try/except — one bad page never kills the whole run.
- BATCH_LIMIT caps how many jobs this run touches, so it's safe to schedule
  on its own timeout and let it catch up gradually across multiple runs.
- Does NOT write an `updated_at` field — that column does not exist on the
  `jobs` table and writing it will make every single save fail with
  "Could not find the 'updated_at' column of 'jobs' in the schema cache".
"""

import os
import re
import json
import asyncio
import logging
import urllib.request

import google.generativeai as genai
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ──────────────────────────────────────────────────────────────
SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_KEY        = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    log.info("✅ Gemini AI ENABLED (primary)")
else:
    gemini_model = None
    log.warning("⚠️  GEMINI_API_KEY not set — will use OpenRouter only")

OPENROUTER_MODEL = "qwen/qwen-2.5-72b-instruct"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Tunables ─────────────────────────────────────────────────────────────────
BATCH_LIMIT      = 200   # jobs processed per run — kept moderate so Gemini's free-tier
                          # rate limit (15 requests/min) isn't exceeded
AI_BATCH_SIZE    = 8     # jobs per AI call
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
#  AI — Gemini (PRIMARY)
# ══════════════════════════════════════════════════════════════════════════════

def _build_prompt(jobs_batch: list[dict]) -> str:
    entries = []
    for i, j in enumerate(jobs_batch):
        raw = j["raw_text"] if j["raw_text"] else "(no content available)"
        needs_title = not j.get("title_ar")
        title_note = (
            " (needs_title_ar: true — this job has NO Arabic title yet, provide one)"
            if needs_title else
            " (needs_title_ar: false — Arabic title already exists, still return your best translation)"
        )
        entries.append(f"### Job {i+1}{title_note}\nEnglish title: {j['title_en']}\nRaw text: {raw}")

    return (
        "You are a professional Arabic HR content writer. For each numbered job below, "
        "provide:\n"
        "1. title_ar: a professional Arabic translation of the English job title\n"
        "2. description_ar: a clean, professional Arabic job description of 120-180 words "
        "based on the raw scraped text. Ignore any navigation menus, ads, or unrelated site "
        "content in the raw text — extract only genuine job information (responsibilities, "
        "requirements, what the role involves). If the raw text has no usable job information, "
        "write a reasonable general Arabic description based on the job title alone.\n\n"
        "Return ONLY a JSON array of objects, one per job, in the same order, each shaped "
        'exactly like {"title_ar": "...", "description_ar": "..."}. No explanations, no '
        "markdown, no extra text — just the raw JSON array.\n\n"
        + "\n\n".join(entries)
    )


def _parse_ai_response(raw_content: str, expected_count: int):
    raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content.strip())
    parsed = json.loads(raw_content)
    if not (isinstance(parsed, list) and len(parsed) == expected_count):
        return None
    results = []
    for x in parsed:
        if isinstance(x, dict):
            results.append({
                "title_ar": str(x.get("title_ar", "")).strip(),
                "description_ar": str(x.get("description_ar", "")).strip(),
            })
        else:
            results.append({"title_ar": "", "description_ar": ""})
    return results


async def ai_batch_gemini(jobs_batch: list[dict]):
    """Use Google Gemini (FREE, primary). Returns None on failure so caller can fall back."""
    if not gemini_model:
        return None

    prompt = _build_prompt(jobs_batch)
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt,
            genai.types.GenerationConfig(temperature=0.3, max_output_tokens=2400),
        )
        results = _parse_ai_response(response.text, len(jobs_batch))
        if results:
            log.info(f"   ✅ Gemini batch successful ({len(results)} jobs)")
            return results
        log.warning("   ⚠ Gemini returned wrong shape, will try OpenRouter")
    except Exception as e:
        log.warning(f"   ⚠ Gemini failed: {e}, will try OpenRouter")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  AI — OpenRouter (FALLBACK)
# ══════════════════════════════════════════════════════════════════════════════

def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


async def ai_batch_openrouter(jobs_batch: list[dict]) -> list[dict]:
    """Fallback: only used if Gemini fails or isn't configured."""
    if not OPENROUTER_API_KEY:
        log.error("   ❌ No OPENROUTER_API_KEY set and Gemini unavailable — cannot process this batch")
        return [{"title_ar": "", "description_ar": ""} for _ in jobs_batch]

    prompt = _build_prompt(jobs_batch)
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2400,
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
            results = _parse_ai_response(raw_content, len(jobs_batch))
            if results:
                log.info(f"   ✅ OpenRouter batch successful ({len(results)} jobs)")
                return results
            log.warning(f"   AI batch returned wrong shape (attempt {attempt}), retrying")
        except Exception as e:
            log.warning(f"   AI batch attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 ** attempt)

    log.error("   ❌ AI batch failed all attempts — leaving these jobs for next run")
    return [{"title_ar": "", "description_ar": ""} for _ in jobs_batch]


async def ai_clean_and_translate_batch(jobs_batch: list[dict]) -> list[dict]:
    """Try Gemini first (FREE), fall back to OpenRouter only if Gemini fails."""
    result = await ai_batch_gemini(jobs_batch)
    if result:
        return result
    log.info("   🔄 Falling back to OpenRouter...")
    return await ai_batch_openrouter(jobs_batch)


# ══════════════════════════════════════════════════════════════════════════════
#  DB — save results
# ══════════════════════════════════════════════════════════════════════════════

def save_description(job_key: str, description_en: str, description_ar: str, title_ar_new: str, title_ar_existing: str) -> bool:
    try:
        update = {
            "description_ar": description_ar,
            "translation_status": "completed",
            # NOTE: deliberately no "updated_at" field here — that column
            # does not exist on the jobs table and including it makes every
            # save fail with "Could not find the 'updated_at' column of
            # 'jobs' in the schema cache". Do not add it back.
        }
        if description_en:
            update["description_en"] = description_en

        if not title_ar_existing and title_ar_new:
            update["title_ar"] = title_ar_new

        supabase.table("jobs").update(update).eq("job_key", job_key).execute()
        return True
    except Exception as e:
        log.error(f"   ❌ DB update failed for {job_key}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA description backfill — starting (Gemini primary, OpenRouter fallback)")
    loop = asyncio.get_event_loop()
    start_time = loop.time()

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
    log.info("\n── Phase 2: AI cleanup & Arabic translation ──")
    saved = failed = 0
    for i in range(0, len(jobs), AI_BATCH_SIZE):
        batch = jobs[i:i + AI_BATCH_SIZE]
        log.info(f"\n  🤖 Processing batch {i // AI_BATCH_SIZE + 1} ({len(batch)} jobs)...")
        ai_results = await ai_clean_and_translate_batch(batch)

        for job, result in zip(batch, ai_results):
            desc_ar = result["description_ar"]
            title_ar_new = result["title_ar"]
            desc_en = job["raw_text"][:600] if job["raw_text"] else ""
            if desc_ar:
                ok = save_description(job["job_key"], desc_en, desc_ar, title_ar_new, job.get("title_ar", ""))
                if ok:
                    title_note = " (+ title_ar filled)" if (not job.get("title_ar") and title_ar_new) else ""
                    log.info(f"    ✅ {job['title_en'][:40]}{title_note}")
                    saved += 1
                else:
                    failed += 1
            else:
                log.warning(f"    ⏭  Skipped (no AI result): {job['title_en'][:40]}")
                failed += 1

        # keeps us safely under Gemini's 15 req/min free-tier limit
        if i + AI_BATCH_SIZE < len(jobs):
            await asyncio.sleep(5)

    elapsed = int(loop.time() - start_time)
    log.info(f"\n{'='*60}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   📦 Processed: {len(jobs)}")
    log.info(f"   ✅ Saved:     {saved}")
    log.info(f"   ❌ Failed:    {failed}")
    log.info(f"   ℹ️  Remaining jobs will be picked up on the next scheduled run")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
