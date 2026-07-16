"""
backfill_mena_descriptions.py
PURPOSE
───────
Fixes the root cause behind the GSC "Discovered - currently not indexed" spike.
LinkedIn and Wuzzuf jobs currently get saved with description_en = "Full details at {url}"
and description_ar is never filled. This script:
1. Finds jobs still stuck with the placeholder description
2. Visits the real source_url and pulls the actual posting text
3. Cleans it (strips nav/footer/ad junk) and asks the AI to turn it into a
   proper ~100-150 word professional Arabic description
4. Updates description_en (real snippet) and description_ar (clean Arabic)

AI STRATEGY
───────────
- PRIMARY: Google Gemini (FREE, fast, excellent Arabic)
- FALLBACK: OpenRouter Qwen 2.5 72B (if Gemini fails)
- Zero cost for most jobs, OpenRouter only used as backup
"""
import os
import re
import json
import asyncio
import logging
import urllib.request
from datetime import datetime
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
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Configure Gemini if available
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
BATCH_LIMIT = 150   # jobs processed per run
AI_BATCH_SIZE = 8   # jobs per AI call
PLACEHOLDER_MARK = "Full details at"
RAW_TEXT_CAP = 2500

JUNK_CUTOFFS = [
    "report this ad", "similar jobs", "related jobs", "وظائف مشابهة",
    "sign in", "create an account", "cookie policy", "privacy policy",
    "all rights reserved", "apply now", "share this job",
]

# ── Text Cleanup ─────────────────────────────────────────────────────────────
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

# ── DB: Fetch jobs needing description ───────────────────────────────────────
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

# ── Scrape: Visit real posting and grab raw text ─────────────────────────────
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
                "div.css-1lh32fc",
                "section.job-description",
            ]

        for sel in candidates:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = await el.inner_text()
                    cleaned = clean_raw_text(text)
                    if len(cleaned) > 80:
                        return cleaned
            except Exception:
                continue

        # Fallback: whole-page text
        body_text = await page.locator("body").inner_text()
        return clean_raw_text(body_text)
    except PlaywrightTimeout:
        log.warning(f"   ⚠ Timeout loading {url}")
        return ""
    except Exception as e:
        log.warning(f"   ⚠ Scrape error {url}: {e}")
        return ""

# ── AI: Gemini (PRIMARY) ─────────────────────────────────────────────────────
async def ai_clean_and_translate_batch_gemini(jobs_batch: list[dict]) -> list[str]:
    """Use Google Gemini to generate Arabic descriptions (FREE)."""
    if not gemini_model:
        return []

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

    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt,
            genai.types.GenerationConfig(temperature=0.3, max_output_tokens=2200)
        )
        raw_content = response.text.strip()
        raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content)
        parsed = json.loads(raw_content)
        if isinstance(parsed, list) and len(parsed) == len(jobs_batch):
            log.info(f"   ✅ Gemini batch successful ({len(parsed)} descriptions)")
            return [str(x).strip() for x in parsed]
        log.warning(f"   ⚠ Gemini returned wrong shape, will try OpenRouter")
    except Exception as e:
        log.warning(f"   ⚠ Gemini failed: {e}, will try OpenRouter")

    return []

# ── AI: OpenRouter (FALLBACK) ────────────────────────────────────────────────
def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

async def ai_clean_and_translate_batch_openrouter(jobs_batch: list[dict]) -> list[str]:
    """Fallback: Use OpenRouter Qwen 2.5 72B if Gemini fails."""
    if not OPENROUTER_API_KEY:
        return []

    entries = []
    for i, j in enumerate(jobs_batch):
        raw = j["raw_text"] if j["raw_text"] else "(no content available)"
        entries.append(f"### Job {i+1}\nTitle: {j['title_en']}\nRaw text: {raw}")

    prompt = (
        "You are a professional Arabic HR content writer. For each numbered job below, "
        "write a clean, professional Arabic job description of 100-150 words based on the "
        "raw scraped text. Ignore any navigation menus, ads, or unrelated site content. "
        "Return ONLY a JSON array of strings, one per job, in the same order.\n\n"
        + "\n\n".join(entries)
    )

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2200,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/mena-jobs-scraper",
        "X-Title": "MENA Jobs Description Backfill",
    }

    for attempt in range(1, 4):
        try:
            resp = http_post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
            raw_content = resp["choices"][0]["message"]["content"].strip()
            raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content.strip())
            parsed = json.loads(raw_content)
            if isinstance(parsed, list) and len(parsed) == len(jobs_batch):
                log.info(f"   ✅ OpenRouter batch successful ({len(parsed)} descriptions)")
                return [str(x).strip() for x in parsed]
            log.warning(f"   OpenRouter returned wrong shape (attempt {attempt}), retrying")
        except Exception as e:
            log.warning(f"   OpenRouter attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 ** attempt)

    log.error("   ❌ Both Gemini and OpenRouter failed for this batch")
    return []

# ── AI: Main dispatcher (tries Gemini first, then OpenRouter) ────────────────
async def ai_clean_and_translate_batch(jobs_batch: list[dict]) -> list[str]:
    """Try Gemini first (FREE), fallback to OpenRouter if it fails."""
    result = await ai_clean_and_translate_batch_gemini(jobs_batch)
    if result:
        return result

    log.info("   🔄 Falling back to OpenRouter...")
    return await ai_clean_and_translate_batch_openrouter(jobs_batch)

# ── DB: Save results (FIXED: removed updated_at) ─────────────────────────────
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

# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 MENA description backfill — starting (Gemini primary, OpenRouter fallback)")
    start = datetime.now()

    jobs = fetch_jobs_needing_description(BATCH_LIMIT)
    log.info(f"📦 {len(jobs)} jobs need real descriptions this run (capped at {BATCH_LIMIT})")

    if not jobs:
        log.info("✅ Nothing to do — all caught up.")
        return

    # ── Phase 1: Scrape real text ────────────────────────────────────────────
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
            await asyncio.sleep(0.5)
        await browser.close()

    scraped_ok = sum(1 for j in jobs if j["raw_text"])
    log.info(f"   ✅ Got real content for {scraped_ok}/{len(jobs)} jobs")

    # ── Phase 2: AI clean + translate ────────────────────────────────────────
    log.info("\n── Phase 2: AI cleanup & Arabic translation ──")
    saved = failed = 0

    for i in range(0, len(jobs), AI_BATCH_SIZE):
        batch = jobs[i:i + AI_BATCH_SIZE]
        log.info(f"\n  🤖 Processing batch {i // AI_BATCH_SIZE + 1} ({len(batch)} jobs)...")

        arabic_descriptions = await ai_clean_and_translate_batch(batch)

        for job, desc_ar in zip(batch, arabic_descriptions):
            desc_en = job["raw_text"][:600] if job["raw_text"] else ""
            if desc_ar:
                ok = save_description(job["job_key"], desc_en, desc_ar)
                if ok:
                    log.info(f"    ✅ {job['title_en'][:40]}")
                    saved += 1
                else:
                    failed += 1
            else:
                log.warning(f"    ⏭  Skipped (no AI result): {job['title_en'][:40]}")
                failed += 1

        if i + AI_BATCH_SIZE < len(jobs):
            await asyncio.sleep(1)

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n{'='*60}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   📦 Processed: {len(jobs)}")
    log.info(f"   ✅ Saved:     {saved}")
    log.info(f"   ❌ Failed:    {failed}")
    log.info(f"   ℹ️  Remaining jobs will be picked up on the next scheduled run")
    log.info(f"{'='*60}")

if __name__ == "__main__":
    asyncio.run(main())
