import os
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
import google.generativeai as genai
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Environment Credentials ────────────────────────────────────────────────────
SUPABASE_URL             = os.environ["SUPABASE_URL"]
SUPABASE_KEY             = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY           = os.environ["GEMINI_API_KEY"]

# ── API Clients ────────────────────────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-1.5-flash")

# ── Constants ──────────────────────────────────────────────────────────────────
MAX_JOBS_PER_SOURCE = 30          # jobs scraped per platform per run
MAX_TRANSLATE_RETRIES = 3         # retry attempts on Gemini failure

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def make_key(platform: str, uid: str) -> str:
    """Deterministic SHA-256 dedup key — guarantees zero duplicate rows."""
    return hashlib.sha256(f"{platform}::{uid}".encode()).hexdigest()


async def translate(text: str, retries: int = MAX_TRANSLATE_RETRIES) -> str:
    """Translate English job text → professional Arabic via Gemini with retry."""
    if not text or not text.strip():
        return ""
    prompt = (
        "You are a professional HR translator specialising in the Arab world. "
        "Translate the following into clear, modern, professional business Arabic "
        "suitable for job seekers across the MENA region. "
        "Preserve technical terms and proper nouns accurately. "
        "Return ONLY the translated text — no explanations, no markdown.\n\n"
        f"{text.strip()}"
    )
    for attempt in range(1, retries + 1):
        try:
            response = ai_model.generate_content(prompt)
            return response.text.strip()
        except Exception as err:
            log.warning(f"Translation attempt {attempt}/{retries} failed: {err}")
            await asyncio.sleep(2 ** attempt)   # exponential back-off
    return ""


def already_exists(key: str) -> bool:
    """Check Supabase for an existing job record by dedup key."""
    result = supabase.table("jobs").select("job_key").eq("job_key", key).execute()
    return len(result.data) > 0


async def save_job(job: dict) -> None:
    """Translate then insert one job record into Supabase."""
    if already_exists(job["job_key"]):
        log.info(f"  ⏭  Skip (exists): {job['title_en'][:60]}")
        return

    log.info(f"  🤖 Translating: {job['title_en'][:60]}")
    job["title_ar"]       = await translate(job["title_en"])
    job["description_ar"] = await translate(job.get("description_en", ""))
    job["translation_status"] = "completed" if job["title_ar"] else "failed"

    try:
        supabase.table("jobs").insert(job).execute()
        log.info(f"  ✅ Saved: {job['title_ar'] or job['title_en']}")
    except Exception as err:
        log.error(f"  ❌ DB insert error: {err}")


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPERS — one async function per platform
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_tanqeeb(ctx) -> list[dict]:
    """Tanqeeb.com — largest Gulf-focused Arabic job board."""
    jobs, page = [], await ctx.new_page()
    platforms = [
        ("SA", "https://www.tanqeeb.com/jobs-in-saudi-arabia"),
        ("AE", "https://www.tanqeeb.com/jobs-in-uae"),
        ("QA", "https://www.tanqeeb.com/jobs-in-qatar"),
        ("EG", "https://www.tanqeeb.com/jobs-in-egypt"),
    ]
    try:
        for country, url in platforms:
            log.info(f"🌐 Tanqeeb → {country} {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
                cards = await page.locator("div.job-card, article.job-item, .job-box, .job_item").all()
                log.info(f"   Found {len(cards)} cards")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title   = (await card.locator("h2, h3, .job-title, .title").first.inner_text()).strip()
                        company = (await card.locator(".company, .company-name, .employer").first.inner_text()).strip()
                        link_el = card.locator("a").first
                        href    = await link_el.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.tanqeeb.com{href}"
                        uid      = href.split("/")[-1].split("?")[0] or title[:40]
                        jobs.append({
                            "job_key":         make_key("tanqeeb", uid),
                            "title_en":        title,
                            "company_name":    company or "Unknown",
                            "description_en":  f"Full details at {full_url}",
                            "country":         country,
                            "source_url":      full_url,
                            "source_platform": "Tanqeeb",
                            "posted_at":       datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout on {url}")
    finally:
        await page.close()
    return jobs


async def scrape_bayt(ctx) -> list[dict]:
    """Bayt.com — premium MENA professional job board."""
    jobs, page = [], await ctx.new_page()
    urls = [
        ("AE", "https://www.bayt.com/en/uae/jobs/"),
        ("SA", "https://www.bayt.com/en/saudi-arabia/jobs/"),
        ("EG", "https://www.bayt.com/en/egypt/jobs/"),
        ("KW", "https://www.bayt.com/en/kuwait/jobs/"),
    ]
    try:
        for country, url in urls:
            log.info(f"🌐 Bayt → {country} {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
                cards = await page.locator("li[data-job-id], .has-pointer-d").all()
                log.info(f"   Found {len(cards)} cards")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title   = (await card.locator("h2, .jb-title").first.inner_text()).strip()
                        company = (await card.locator(".jb-company, .t-default").first.inner_text()).strip()
                        href    = await card.locator("a").first.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.bayt.com{href}"
                        uid      = href.split("/")[-2] or title[:40]
                        jobs.append({
                            "job_key":         make_key("bayt", uid),
                            "title_en":        title,
                            "company_name":    company or "Unknown",
                            "description_en":  f"Full details at {full_url}",
                            "country":         country,
                            "source_url":      full_url,
                            "source_platform": "Bayt",
                            "posted_at":       datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout on {url}")
    finally:
        await page.close()
    return jobs


async def scrape_wuzzuf(ctx) -> list[dict]:
    """Wuzzuf.net — Egypt's #1 job board."""
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://wuzzuf.net/search/jobs/?q=&a=hpb"
        log.info(f"🌐 Wuzzuf → EG {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        cards = await page.locator("div.css-1gatmva, div[data-results-list] > div").all()
        log.info(f"   Found {len(cards)} cards")
        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title   = (await card.locator("h2 a, .css-m604qf").first.inner_text()).strip()
                company = (await card.locator(".css-17s97q8, a.css-17s97q8").first.inner_text()).strip()
                href    = await card.locator("h2 a, a.css-o171kl").first.get_attribute("href") or ""
                full_url = f"https://wuzzuf.net{href}" if not href.startswith("http") else href
                uid      = href.split("/")[-1].split("?")[0] or title[:40]
                jobs.append({
                    "job_key":         make_key("wuzzuf", uid),
                    "title_en":        title,
                    "company_name":    company or "Unknown",
                    "description_en":  f"Full details at {full_url}",
                    "country":         "EG",
                    "source_url":      full_url,
                    "source_platform": "Wuzzuf",
                    "posted_at":       datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.debug(f"   Card error: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout on Wuzzuf")
    finally:
        await page.close()
    return jobs


async def scrape_linkedin_mena(ctx) -> list[dict]:
    """LinkedIn public job search — MENA filter (no login required)."""
    jobs, page = [], await ctx.new_page()
    searches = [
        ("AE", "https://www.linkedin.com/jobs/search/?location=United%20Arab%20Emirates&f_TPR=r86400"),
        ("SA", "https://www.linkedin.com/jobs/search/?location=Saudi%20Arabia&f_TPR=r86400"),
        ("MA", "https://www.linkedin.com/jobs/search/?location=Morocco&f_TPR=r86400"),
        ("EG", "https://www.linkedin.com/jobs/search/?location=Egypt&f_TPR=r86400"),
    ]
    try:
        for country, url in searches:
            log.info(f"🌐 LinkedIn → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)
                # Scroll to load more results
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                await page.wait_for_timeout(2000)
                cards = await page.locator("div.base-card, li.jobs-search-results__list-item").all()
                log.info(f"   Found {len(cards)} cards")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title   = (await card.locator("h3, .base-search-card__title").first.inner_text()).strip()
                        company = (await card.locator("h4, .base-search-card__subtitle").first.inner_text()).strip()
                        href    = await card.locator("a").first.get_attribute("href") or ""
                        uid     = href.split("/")[-1].split("?")[0] or title[:40]
                        jobs.append({
                            "job_key":         make_key("linkedin", uid),
                            "title_en":        title,
                            "company_name":    company or "Unknown",
                            "description_en":  f"Full details at {href}",
                            "country":         country,
                            "source_url":      href,
                            "source_platform": "LinkedIn",
                            "posted_at":       datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout on LinkedIn {country}")
    finally:
        await page.close()
    return jobs


async def scrape_dreamjob(ctx) -> list[dict]:
    """Dreamjob.ma — Morocco's leading job board."""
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://www.dreamjob.ma/offres-emploi/"
        log.info(f"🌐 Dreamjob.ma → MA {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        cards = await page.locator("div.job-listing, article.job_listing, .job-item").all()
        log.info(f"   Found {len(cards)} cards")
        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title   = (await card.locator("h3, h2, .job-title").first.inner_text()).strip()
                company = (await card.locator(".company, .company-name, strong").first.inner_text()).strip()
                href    = await card.locator("a").first.get_attribute("href") or ""
                full_url = href if href.startswith("http") else f"https://www.dreamjob.ma{href}"
                uid     = href.split("/")[-2] or title[:40]
                # Dreamjob lists French/Arabic — translate title anyway
                jobs.append({
                    "job_key":         make_key("dreamjob", uid),
                    "title_en":        title,
                    "company_name":    company or "Unknown",
                    "description_en":  f"Full details at {full_url}",
                    "country":         "MA",
                    "source_url":      full_url,
                    "source_platform": "Dreamjob.ma",
                    "posted_at":       datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.debug(f"   Card error: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout on Dreamjob.ma")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA Jobs Scraper — pipeline starting")
    start = datetime.now()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )

        # Run all scrapers — gather raw jobs lists
        scrapers = [
            scrape_tanqeeb(ctx),
            scrape_bayt(ctx),
            scrape_wuzzuf(ctx),
            scrape_linkedin_mena(ctx),
            scrape_dreamjob(ctx),
        ]
        results = await asyncio.gather(*scrapers, return_exceptions=True)
        await browser.close()

    # Flatten results, skip any scraper that threw an exception
    all_jobs: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            log.error(f"Scraper failed: {r}")
        else:
            all_jobs.extend(r)

    log.info(f"\n📦 Total raw jobs collected: {len(all_jobs)}")

    # Translate + save sequentially (respect Gemini rate limits)
    saved = 0
    for job in all_jobs:
        await save_job(job)
        saved += 1
        await asyncio.sleep(0.5)   # gentle rate-limit buffer

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n🏁 Done — {saved} jobs processed in {elapsed}s")


if __name__ == "__main__":
    asyncio.run(main())
