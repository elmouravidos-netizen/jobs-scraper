import os
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client, Client

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Gemini is optional — won't crash if missing
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TRANSLATE_ENABLED = bool(GEMINI_API_KEY)

if TRANSLATE_ENABLED:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel("gemini-1.5-flash")
    log.info("✅ Gemini translation ENABLED")
else:
    log.info("⏭  Gemini translation SKIPPED (no API key)")

# ── Supabase Client ────────────────────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_JOBS_PER_SOURCE = 30

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def make_key(platform: str, uid: str) -> str:
    return hashlib.sha256(f"{platform}::{uid}".encode()).hexdigest()


async def translate(text: str) -> str:
    if not TRANSLATE_ENABLED or not text.strip():
        return ""
    try:
        prompt = (
            "You are a professional HR translator for the Arab world. "
            "Translate into clear, modern business Arabic for MENA job seekers. "
            "Preserve technical terms. Return ONLY the translated text.\n\n"
            f"{text.strip()}"
        )
        for attempt in range(1, 4):
            try:
                response = ai_model.generate_content(prompt)
                return response.text.strip()
            except Exception as err:
                log.warning(f"Translation attempt {attempt}/3 failed: {err}")
                await asyncio.sleep(2 ** attempt)
    except Exception as e:
        log.warning(f"Translation skipped: {e}")
    return ""


def already_exists(key: str) -> bool:
    result = supabase.table("jobs").select("job_key").eq("job_key", key).execute()
    return len(result.data) > 0


async def save_job(job: dict) -> None:
    if already_exists(job["job_key"]):
        log.info(f"  ⏭  Skip (exists): {job['title_en'][:60]}")
        return

    # Translate if Gemini is available, otherwise save as pending
    if TRANSLATE_ENABLED:
        job["title_ar"]       = await translate(job["title_en"])
        job["description_ar"] = await translate(job.get("description_en", ""))
        job["translation_status"] = "completed" if job["title_ar"] else "failed"
    else:
        job["title_ar"]           = ""
        job["description_ar"]     = ""
        job["translation_status"] = "pending"

    try:
        supabase.table("jobs").insert(job).execute()
        log.info(f"  ✅ Saved: {job['title_en'][:60]}")
    except Exception as err:
        log.error(f"  ❌ DB error: {err}")


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPERS
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_tanqeeb(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("SA", "https://www.tanqeeb.com/jobs-in-saudi-arabia"),
        ("AE", "https://www.tanqeeb.com/jobs-in-uae"),
        ("QA", "https://www.tanqeeb.com/jobs-in-qatar"),
        ("EG", "https://www.tanqeeb.com/jobs-in-egypt"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 Tanqeeb → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
                cards = await page.locator("div.job-card, article.job-item, .job-box, .job_item").all()
                log.info(f"   Found {len(cards)} cards")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title   = (await card.locator("h2, h3, .job-title, .title").first.inner_text()).strip()
                        company = (await card.locator(".company, .company-name, .employer").first.inner_text()).strip()
                        href    = await card.locator("a").first.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.tanqeeb.com{href}"
                        uid     = href.split("/")[-1].split("?")[0] or title[:40]
                        if not title: continue
                        jobs.append({
                            "job_key": make_key("tanqeeb", uid), "title_en": title,
                            "company_name": company or "Unknown", "description_en": f"Full details at {full_url}",
                            "country": country, "source_url": full_url, "source_platform": "Tanqeeb",
                            "posted_at": datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: {url}")
    finally:
        await page.close()
    return jobs


async def scrape_bayt(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("AE", "https://www.bayt.com/en/uae/jobs/"),
        ("SA", "https://www.bayt.com/en/saudi-arabia/jobs/"),
        ("EG", "https://www.bayt.com/en/egypt/jobs/"),
        ("KW", "https://www.bayt.com/en/kuwait/jobs/"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 Bayt → {country}")
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
                        uid     = href.split("/")[-2] or title[:40]
                        if not title: continue
                        jobs.append({
                            "job_key": make_key("bayt", uid), "title_en": title,
                            "company_name": company or "Unknown", "description_en": f"Full details at {full_url}",
                            "country": country, "source_url": full_url, "source_platform": "Bayt",
                            "posted_at": datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: {url}")
    finally:
        await page.close()
    return jobs


async def scrape_wuzzuf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://wuzzuf.net/search/jobs/?q=&a=hpb"
        log.info(f"🌐 Wuzzuf → EG")
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
                uid     = href.split("/")[-1].split("?")[0] or title[:40]
                if not title: continue
                jobs.append({
                    "job_key": make_key("wuzzuf", uid), "title_en": title,
                    "company_name": company or "Unknown", "description_en": f"Full details at {full_url}",
                    "country": "EG", "source_url": full_url, "source_platform": "Wuzzuf",
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.debug(f"   Card error: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Wuzzuf")
    finally:
        await page.close()
    return jobs


async def scrape_linkedin_mena(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("AE", "https://www.linkedin.com/jobs/search/?location=United%20Arab%20Emirates&f_TPR=r86400"),
        ("SA", "https://www.linkedin.com/jobs/search/?location=Saudi%20Arabia&f_TPR=r86400"),
        ("MA", "https://www.linkedin.com/jobs/search/?location=Morocco&f_TPR=r86400"),
        ("EG", "https://www.linkedin.com/jobs/search/?location=Egypt&f_TPR=r86400"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 LinkedIn → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)
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
                        if not title: continue
                        jobs.append({
                            "job_key": make_key("linkedin", uid), "title_en": title,
                            "company_name": company or "Unknown", "description_en": f"Full details at {href}",
                            "country": country, "source_url": href, "source_platform": "LinkedIn",
                            "posted_at": datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        log.debug(f"   Card error: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: LinkedIn {country}")
    finally:
        await page.close()
    return jobs


async def scrape_dreamjob(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://www.dreamjob.ma/offres-emploi/"
        log.info(f"🌐 Dreamjob.ma → MA")
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
                if not title: continue
                jobs.append({
                    "job_key": make_key("dreamjob", uid), "title_en": title,
                    "company_name": company or "Unknown", "description_en": f"Full details at {full_url}",
                    "country": "MA", "source_url": full_url, "source_platform": "Dreamjob.ma",
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.debug(f"   Card error: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Dreamjob.ma")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA Jobs Scraper — starting pipeline")
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

        results = await asyncio.gather(
            scrape_tanqeeb(ctx),
            scrape_bayt(ctx),
            scrape_wuzzuf(ctx),
            scrape_linkedin_mena(ctx),
            scrape_dreamjob(ctx),
            return_exceptions=True
        )
        await browser.close()

    all_jobs: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            log.error(f"Scraper exception: {r}")
        else:
            all_jobs.extend(r)

    log.info(f"\n📦 Total jobs collected: {len(all_jobs)}")

    saved = skipped = failed = 0
    for job in all_jobs:
        try:
            before = skipped
            await save_job(job)
            if skipped == before:
                saved += 1
        except Exception as e:
            log.error(f"Pipeline error: {e}")
            failed += 1

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n🏁 Done in {elapsed}s — ✅ {saved} saved | ⏭ {skipped} skipped | ❌ {failed} failed")

if __name__ == "__main__":
    asyncio.run(main())
