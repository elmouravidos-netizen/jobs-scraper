"""
INTERNATIONAL VISA-SPONSORED JOBS SCRAPER
-------------------------------------------
Scrapes visasponsor.jobs for English-language jobs with
guaranteed visa sponsorship — targeting Arab job seekers
looking for international work contracts.

Runs SEPARATELY from the main MENA scraper (scraper.py):
- Different schedule (daily, not every 6 hours)
- Different data source (Playwright scrape, not APIs)
- Shares utilities via scraper_shared.py
- Cannot break or block the main scraper if it fails

Both title AND description are translated to Arabic
(unlike MENA jobs where only titles are translated) because:
1. These are lower volume, higher value listings
2. Full descriptions are real and unique — translating them
   avoids duplicate content risk and adds genuine value
3. Visa/program details matter and must be clear in Arabic
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timezone
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

from scraper_shared import (
    make_key, clean_url, detect_work_mode, detect_category,
    translate_title_and_description, filter_new_jobs,
)

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
log.info(f"{'✅' if TRANSLATE_ENABLED else '⏭ '} OpenRouter translation {'ENABLED' if TRANSLATE_ENABLED else 'SKIPPED'}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_PER_COUNTRY     = 30   # jobs scraped per country per run
MAX_DESC_FETCHES    = 50   # cap on detail-page visits per run (prevents timeout)
DESC_FETCH_TIMEOUT  = 15   # seconds per detail page — fail fast, don't hang

# ── Target countries — matches your homepage country focus ────────────────────
COUNTRIES = [
    ("DE", "Germany"),
    ("CA", "Canada"),
    ("GB", "United Kingdom"),
    ("AU", "Australia"),
]

# ── Visa type Arabic translation dictionary (fixed, not AI — more accurate) ───
VISA_TYPE_AR = {
    'EU Blue Card':            'البطاقة الزرقاء الأوروبية',
    'Skilled Worker':          'تأشيرة العامل الماهر',
    'Health and Care Worker':  'تأشيرة العاملين الصحيين',
    'PNP':                     'برنامج الترشيح الإقليمي الكندي',
    'TFWP':                    'برنامج العمال الأجانب المؤقتين',
    'Critical Skills':         'تأشيرة المهارات الحرجة',
    '186':                     'تأشيرة أصحاب العمل الدائمة 186',
    '482':                     'تأشيرة المهارات المؤقتة 482',
    '485':                     'تأشيرة التخرج المؤقتة 485',
}


def translate_visa_type(visa_raw: str) -> str:
    """Map known visa program names to Arabic, fallback to generic label."""
    if not visa_raw:
        return ''
    for en, ar in VISA_TYPE_AR.items():
        if en.lower() in visa_raw.lower():
            return ar
    return 'تأشيرة عمل برعاية صاحب العمل'  # generic fallback


def build_intl_job(uid: str, title: str, company: str, country: str,
                    url: str, description: str, visa_type: str,
                    experience_level: str = '') -> dict:
    """Build a job dict for international contract jobs."""
    c = clean_url(url)
    return {
        "job_key":            make_key("visasponsor", uid),
        "title_en":           title.strip(),
        "company_name":       (company or "Unknown").strip(),
        "description_en":     description.strip()[:2000] if description else f"Full details at {c}",
        "title_ar":           "",
        "description_ar":     "",
        "translation_status": "pending",
        "country":            country,
        "location_city":      "",
        "work_mode":          detect_work_mode(title, description),
        "job_category":       detect_category(title),
        "salary_range":       "",
        "source_url":         c,
        "source_platform":    "VisaSponsor",
        "job_type":           "international_contract",
        "visa_type":          visa_type,
        "visa_type_ar":       translate_visa_type(visa_type),
        "experience_level":   experience_level,
        "is_active":          True,
        "posted_at":          datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER — visasponsor.jobs
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_country(ctx, country_code: str, country_name: str) -> list[dict]:
    """Scrape one country's visa-sponsored job listings."""
    jobs, page = [], await ctx.new_page()
    url = f"https://visasponsor.jobs/api/jobs?country={country_name.replace(' ', '%20')}"

    try:
        log.info(f"🌐 VisaSponsor → {country_name}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)

        # Job cards are <a> links wrapping each listing
        cards = await page.locator("a[href*='/api/jobs/']").all()
        log.info(f"   {len(cards)} cards found")

        for card in cards[:MAX_PER_COUNTRY]:
            try:
                href = await card.get_attribute("href") or ""
                full_url = href if href.startswith("http") else f"https://visasponsor.jobs{href}"

                # Extract job ID from URL: /api/jobs/{32-char-id}/{slug}
                id_match = re.search(r'/api/jobs/([a-f0-9]+)/', full_url)
                uid = id_match.group(1) if id_match else full_url[-40:]

                card_text = (await card.inner_text()).strip()
                if not card_text:
                    continue

                # Card text typically: "Title\nCompany\nLocation\nCategory\nVisa info\nDate"
                lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                title = lines[0] if lines else ""
                company = lines[1] if len(lines) > 1 else ""

                # Detect visa type from card text (look for known program keywords)
                visa_type = ""
                for keyword in VISA_TYPE_AR.keys():
                    if keyword.lower() in card_text.lower():
                        visa_type = keyword
                        break

                if title and len(title) > 3:
                    jobs.append(build_intl_job(
                        uid=uid,
                        title=title,
                        company=company,
                        country=country_code,
                        url=full_url,
                        description="",  # fetched in detail pass below
                        visa_type=visa_type,
                    ))

            except Exception as e:
                log.debug(f"   card err: {e}")

    except PlaywrightTimeout:
        log.warning(f"   ⚠ Timeout: VisaSponsor {country_name}")
    finally:
        await page.close()

    return jobs


async def fetch_job_description(ctx, job: dict) -> str:
    """Fetch the full description from a job's detail page.
    Uses a short timeout — fails fast rather than hanging the whole run."""
    page = await ctx.new_page()
    try:
        await page.goto(
            job["source_url"],
            wait_until="domcontentloaded",
            timeout=DESC_FETCH_TIMEOUT * 1000
        )
        await page.wait_for_timeout(1000)

        # Try common description container selectors
        for sel in ["main", "article", "[class*='description']", "[class*='content']"]:
            try:
                text = await page.locator(sel).first.inner_text(timeout=3000)
                if text and len(text) > 200:
                    return text.strip()[:2000]
            except Exception:
                continue

        return ""
    except (PlaywrightTimeout, Exception) as e:
        log.debug(f"   description fetch timeout/err: {e}")
        return ""
    finally:
        await page.close()


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSLATE + SAVE
# ══════════════════════════════════════════════════════════════════════════════

async def translate_and_save(new_jobs: list[dict]) -> tuple[int, int]:
    """Translate title+description for each new international job, then save."""
    saved = failed = 0

    for job in new_jobs:
        if TRANSLATE_ENABLED:
            title_ar, desc_ar = await translate_title_and_description(
                job["title_en"], job["description_en"], OPENROUTER_API_KEY
            )
            job["title_ar"] = title_ar
            job["description_ar"] = desc_ar
            job["translation_status"] = "completed" if title_ar else "pending"
        else:
            job["translation_status"] = "pending"

        try:
            supabase.table("jobs").insert(job).execute()
            log.info(f"  ✅ [{job['country']}][{job['job_category']:12}] {job['title_en'][:40]} → {job['title_ar'][:30] if job['title_ar'] else '(pending)'}")
            saved += 1
        except Exception as e:
            log.error(f"  ❌ DB error: {e}")
            failed += 1

        # gentle pacing between translation calls
        await asyncio.sleep(0.5)

    return saved, failed


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🌍 International Visa-Sponsored Jobs Scraper — starting")
    start = datetime.now()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )

        # ── Phase 1: Collect job listings from all target countries ──────────
        all_jobs: list[dict] = []
        for code, name in COUNTRIES:
            country_jobs = await scrape_country(ctx, code, name)
            all_jobs.extend(country_jobs)

        log.info(f"\n📦 Total collected: {len(all_jobs)}")

        # ── Phase 2: Dedup against existing DB ────────────────────────────────
        new_jobs = filter_new_jobs(all_jobs, supabase)
        log.info(f"🆕 New jobs: {len(new_jobs)}")

        # ── Phase 3: Fetch full descriptions for new jobs only ────────────────
        # Capped at MAX_DESC_FETCHES to prevent GitHub Actions timeout —
        # jobs beyond the cap still get saved, just without full description
        log.info(f"\n── Fetching full descriptions (capped at {MAX_DESC_FETCHES}) ──")
        fetch_count = min(len(new_jobs), MAX_DESC_FETCHES)
        for i, job in enumerate(new_jobs[:fetch_count]):
            log.info(f"   [{i+1}/{fetch_count}] {job['title_en'][:50]}")
            desc = await fetch_job_description(ctx, job)
            if desc:
                job["description_en"] = desc
        if len(new_jobs) > MAX_DESC_FETCHES:
            log.info(f"   ⏭  {len(new_jobs) - MAX_DESC_FETCHES} jobs will use card preview text only (cap reached)")

        await browser.close()

    # ── Phase 4: Translate + Save ──────────────────────────────────────────────
    log.info("\n── Translating and saving ──")
    saved, failed = await translate_and_save(new_jobs) if new_jobs else (0, 0)

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n{'='*55}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   📦 Collected: {len(all_jobs)}")
    log.info(f"   🆕 New:       {len(new_jobs)}")
    log.info(f"   ✅ Saved:     {saved}")
    log.info(f"   ❌ Failed:    {failed}")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    asyncio.run(main())
