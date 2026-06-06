import os
import re
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TRANSLATE_ENABLED = bool(GEMINI_API_KEY)

if TRANSLATE_ENABLED:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel("gemini-1.5-flash")
    log.info("✅ Gemini translation ENABLED")
else:
    log.info("⏭  Gemini translation SKIPPED (no API key)")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_JOBS_PER_SOURCE = 30

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def make_key(platform: str, uid: str) -> str:
    return hashlib.sha256(f"{platform}::{uid}".encode()).hexdigest()


def clean_url(url: str) -> str:
    """Strip tracking params, keep only clean path."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if "linkedin.com" in parsed.netloc:
            match = re.search(r'/jobs/view/[^/?]+', parsed.path)
            if match:
                return f"https://www.linkedin.com{match.group(0)}"
        tracking = {'trackingId', 'refId', 'pageNum', 'position', 'searchId', 'trk'}
        qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in tracking}
        clean = parsed._replace(query=urlencode(qs, doseq=True))
        return urlunparse(clean)
    except Exception:
        return url


def detect_work_mode(title: str, description: str = "") -> str:
    text = (title + " " + description).lower()
    if any(w in text for w in ['remote', '100% remote', 'fully remote', 'work from home', 'wfh', 'télétravail']):
        return 'Remote'
    if any(w in text for w in ['hybrid', 'hybride', 'flexible']):
        return 'Hybrid'
    return 'Onsite'


def detect_category(title: str) -> str:
    title_lower = title.lower()
    categories = {
        'Technology':       ['developer', 'engineer', 'software', 'data', 'devops', 'cloud', 'cyber', 'it ', 'tech', 'programmer', 'fullstack', 'frontend', 'backend', 'mobile', 'ai ', 'ml ', 'architect'],
        'Sales':            ['sales', 'account manager', 'business development', 'bd ', 'revenue', 'commercial'],
        'Marketing':        ['marketing', 'seo', 'content', 'social media', 'brand', 'digital', 'media buyer'],
        'Finance':          ['finance', 'accounting', 'accountant', 'auditor', 'tax', 'treasury', 'financial', 'cfo'],
        'HR':               ['hr ', 'human resources', 'talent', 'recruiter', 'recruitment', 'payroll'],
        'Operations':       ['operations', 'logistics', 'supply chain', 'procurement', 'warehouse', 'inventory'],
        'Healthcare':       ['doctor', 'nurse', 'pharmacist', 'medical', 'health', 'clinical', 'dentist', 'sage femme'],
        'Education':        ['teacher', 'instructor', 'professor', 'tutor', 'trainer', 'educational'],
        'Design':           ['designer', 'ux', 'ui ', 'graphic', 'creative', 'visual'],
        'Customer Service': ['customer', 'support', 'helpdesk', 'call center', 'client'],
        'Management':       ['manager', 'director', 'head of', 'chief', 'ceo', 'cto', 'vp ', 'vice president'],
        'Engineering':      ['mechanical', 'electrical', 'civil', 'chemical', 'industrial', 'construction', 'maintenance'],
    }
    for category, keywords in categories.items():
        if any(kw in title_lower for kw in keywords):
            return category
    return 'Other'


def already_exists(key: str) -> bool:
    result = supabase.table("jobs").select("job_key").eq("job_key", key).execute()
    return len(result.data) > 0


async def translate(text: str) -> str:
    if not TRANSLATE_ENABLED or not text.strip():
        return ""
    prompt = (
        "You are a professional HR translator for the Arab world. "
        "Translate the following into clear, modern business Arabic for MENA job seekers. "
        "Preserve technical terms. Return ONLY the translated text.\n\n"
        f"{text.strip()}"
    )
    for attempt in range(1, 4):
        try:
            response = ai_model.generate_content(prompt)
            return response.text.strip()
        except Exception as err:
            log.warning(f"Translation attempt {attempt}/3: {err}")
            await asyncio.sleep(2 ** attempt)
    return ""


async def save_job(job: dict) -> bool:
    if already_exists(job["job_key"]):
        log.info(f"  ⏭  Skip: {job['title_en'][:55]}")
        return False

    if TRANSLATE_ENABLED:
        job["title_ar"] = await translate(job["title_en"])
        job["description_ar"] = await translate(job.get("description_en", ""))
        job["translation_status"] = "completed" if job["title_ar"] else "failed"
    else:
        job["title_ar"] = ""
        job["description_ar"] = ""
        job["translation_status"] = "pending"

    try:
        supabase.table("jobs").insert(job).execute()
        log.info(f"  ✅ [{job['source_platform']:12}] [{job['country']}] [{job['job_category']:15}] [{job['work_mode']:7}] {job['title_en'][:45]}")
        return True
    except Exception as err:
        log.error(f"  ❌ DB error: {err}")
        return False


def build_job(platform, uid, title, company, country, url, description="") -> dict:
    clean = clean_url(url)
    return {
        "job_key":            make_key(platform.lower(), uid),
        "title_en":           title.strip(),
        "company_name":       company.strip() if company else "Unknown",
        "description_en":     description or f"Full details at {clean}",
        "title_ar":           "",
        "description_ar":     "",
        "translation_status": "pending",
        "country":            country,
        "location_city":      "",
        "work_mode":          detect_work_mode(title, description),
        "job_category":       detect_category(title),
        "salary_range":       "",
        "source_url":         clean,
        "source_platform":    platform,
        "is_active":          True,
        "posted_at":          datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 1 — LinkedIn (working + paginated for more results)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_linkedin(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("AE", "https://www.linkedin.com/jobs/search/?location=United%20Arab%20Emirates&f_TPR=r86400&start=0"),
        ("AE", "https://www.linkedin.com/jobs/search/?location=United%20Arab%20Emirates&f_TPR=r86400&start=25"),
        ("SA", "https://www.linkedin.com/jobs/search/?location=Saudi%20Arabia&f_TPR=r86400&start=0"),
        ("SA", "https://www.linkedin.com/jobs/search/?location=Saudi%20Arabia&f_TPR=r86400&start=25"),
        ("MA", "https://www.linkedin.com/jobs/search/?location=Morocco&f_TPR=r86400&start=0"),
        ("MA", "https://www.linkedin.com/jobs/search/?location=Morocco&f_TPR=r86400&start=25"),
        ("EG", "https://www.linkedin.com/jobs/search/?location=Egypt&f_TPR=r86400&start=0"),
        ("EG", "https://www.linkedin.com/jobs/search/?location=Egypt&f_TPR=r86400&start=25"),
        ("QA", "https://www.linkedin.com/jobs/search/?location=Qatar&f_TPR=r86400&start=0"),
        ("KW", "https://www.linkedin.com/jobs/search/?location=Kuwait&f_TPR=r86400&start=0"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 LinkedIn → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
                await page.wait_for_timeout(2000)
                cards = await page.locator("div.base-card").all()
                log.info(f"   {len(cards)} cards found")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title   = (await card.locator(".base-search-card__title").inner_text()).strip()
                        company = (await card.locator(".base-search-card__subtitle").inner_text()).strip()
                        href    = await card.locator("a.base-card__full-link").get_attribute("href") or ""
                        uid     = re.search(r'/jobs/view/(\d+)', href)
                        uid     = uid.group(1) if uid else href[-20:]
                        if not title:
                            continue
                        jobs.append(build_job("LinkedIn", uid, title, company, country, href))
                    except Exception as e:
                        log.debug(f"   card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: LinkedIn {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 2 — Bayt.com
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_bayt(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("AE", "https://www.bayt.com/en/uae/jobs/"),
        ("SA", "https://www.bayt.com/en/saudi-arabia/jobs/"),
        ("EG", "https://www.bayt.com/en/egypt/jobs/"),
        ("KW", "https://www.bayt.com/en/kuwait/jobs/"),
        ("QA", "https://www.bayt.com/en/qatar/jobs/"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 Bayt → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                cards = await page.locator("li[data-job-id]").all()
                if not cards:
                    cards = await page.locator("ul.list li").all()
                log.info(f"   {len(cards)} cards found")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title_el = card.locator("h2.jb-title a, h2 a, [class*='title'] a").first
                        title    = (await title_el.inner_text()).strip()
                        company  = ""
                        try:
                            company = (await card.locator("[class*='company'], [class*='employer']").first.inner_text()).strip()
                        except Exception:
                            pass
                        href     = await title_el.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.bayt.com{href}"
                        uid      = href.split("/")[-2] or title[:40]
                        if not title:
                            continue
                        jobs.append(build_job("Bayt", uid, title, company, country, full_url))
                    except Exception as e:
                        log.debug(f"   card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Bayt {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 3 — Wuzzuf (Egypt)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_wuzzuf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://wuzzuf.net/search/jobs/?q=&a=hpb"
        log.info(f"🌐 Wuzzuf → EG")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        cards = await page.locator("div.css-1gatmva").all()
        if not cards:
            cards = await page.locator("article, [class*='JobCard'], [class*='job-card']").all()
        log.info(f"   {len(cards)} cards found")
        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title_el = card.locator("h2 a").first
                title    = (await title_el.inner_text()).strip()
                company  = ""
                try:
                    company = (await card.locator("a[class*='company'], [class*='company']").first.inner_text()).strip()
                except Exception:
                    pass
                href     = await title_el.get_attribute("href") or ""
                full_url = f"https://wuzzuf.net{href}" if not href.startswith("http") else href
                uid      = re.sub(r'\?.*', '', href).split("/")[-1] or title[:40]
                if not title:
                    continue
                jobs.append(build_job("Wuzzuf", uid, title, company, "EG", full_url))
            except Exception as e:
                log.debug(f"   card err: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Wuzzuf")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 4 — Tanqeeb
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_tanqeeb(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("SA", "https://www.tanqeeb.com/jobs-in-saudi-arabia"),
        ("AE", "https://www.tanqeeb.com/jobs-in-uae"),
        ("EG", "https://www.tanqeeb.com/jobs-in-egypt"),
        ("MA", "https://www.tanqeeb.com/jobs-in-morocco"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 Tanqeeb → {country}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=50000)
                await page.wait_for_timeout(5000)
                selectors = [
                    "div.job-card",
                    "div[class*='job-card']",
                    "article[class*='job']",
                    "div[class*='JobCard']",
                    "div[class*='vacancy']",
                    ".job-listing",
                    "div[data-job]",
                ]
                cards = []
                for sel in selectors:
                    cards = await page.locator(sel).all()
                    if cards:
                        log.info(f"   Matched: {sel}")
                        break
                log.info(f"   {len(cards)} cards found")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title_el = card.locator("h2, h3, [class*='title']").first
                        title    = (await title_el.inner_text()).strip()
                        company  = ""
                        try:
                            company = (await card.locator("[class*='company'], [class*='employer']").first.inner_text()).strip()
                        except Exception:
                            pass
                        href = ""
                        try:
                            href = await card.locator("a").first.get_attribute("href") or ""
                        except Exception:
                            pass
                        full_url = href if href.startswith("http") else f"https://www.tanqeeb.com{href}"
                        uid      = re.sub(r'\?.*', '', href).split("/")[-1] or title[:40]
                        if not title:
                            continue
                        jobs.append(build_job("Tanqeeb", uid, title, company, country, full_url))
                    except Exception as e:
                        log.debug(f"   card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Tanqeeb {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 5 — Dreamjob.ma (Morocco)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_dreamjob(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://www.dreamjob.ma/offres-emploi/"
        log.info(f"🌐 Dreamjob.ma → MA")
        await page.goto(url, wait_until="networkidle", timeout=50000)
        await page.wait_for_timeout(4000)
        selectors = [
            "li.job_listing",
            "div.job_listing",
            "[class*='job_listing']",
            "article[class*='job']",
            ".jobList li",
            "div.job-item",
        ]
        cards = []
        for sel in selectors:
            cards = await page.locator(sel).all()
            if cards:
                log.info(f"   Matched: {sel}")
                break
        log.info(f"   {len(cards)} cards found")
        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title_el = card.locator("h3 a, h2 a, a[class*='title'], .position a").first
                title    = (await title_el.inner_text()).strip()
                company  = ""
                try:
                    company = (await card.locator(".company, strong, [class*='company']").first.inner_text()).strip()
                except Exception:
                    pass
                href     = await title_el.get_attribute("href") or ""
                full_url = href if href.startswith("http") else f"https://www.dreamjob.ma{href}"
                uid      = re.sub(r'\?.*', '', href).split("/")[-2] or title[:40]
                if not title:
                    continue
                jobs.append(build_job("Dreamjob.ma", uid, title, company, "MA", full_url))
            except Exception as e:
                log.debug(f"   card err: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Dreamjob.ma")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER 6 — Naukrigulf (bonus Gulf source)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_naukrigulf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    targets = [
        ("AE", "https://www.naukrigulf.com/jobs-in-uae"),
        ("SA", "https://www.naukrigulf.com/jobs-in-saudi-arabia"),
        ("QA", "https://www.naukrigulf.com/jobs-in-qatar"),
    ]
    try:
        for country, url in targets:
            log.info(f"🌐 Naukrigulf → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                cards = await page.locator("div.ni-job-tuple, [class*='jobTuple'], [class*='job-tuple']").all()
                if not cards:
                    cards = await page.locator("div[class*='JobCard'], section[class*='job']").all()
                log.info(f"   {len(cards)} cards found")
                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title_el = card.locator("a[class*='title'], h3 a, h2 a").first
                        title    = (await title_el.inner_text()).strip()
                        company  = ""
                        try:
                            company = (await card.locator("[class*='comp-name'], [class*='company']").first.inner_text()).strip()
                        except Exception:
                            pass
                        href     = await title_el.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.naukrigulf.com{href}"
                        uid      = re.sub(r'\?.*', '', href).split("-")[-1] or title[:40]
                        if not title:
                            continue
                        jobs.append(build_job("Naukrigulf", uid, title, company, country, full_url))
                    except Exception as e:
                        log.debug(f"   card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Naukrigulf {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA Jobs Scraper v2 — starting")
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
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        results = await asyncio.gather(
            scrape_linkedin(ctx),
            scrape_bayt(ctx),
            scrape_wuzzuf(ctx),
            scrape_tanqeeb(ctx),
            scrape_dreamjob(ctx),
            scrape_naukrigulf(ctx),
            return_exceptions=True
        )
        await browser.close()

    all_jobs: list[dict] = []
    scraper_names = ["LinkedIn", "Bayt", "Wuzzuf", "Tanqeeb", "Dreamjob", "Naukrigulf"]
    for name, r in zip(scraper_names, results):
        if isinstance(r, Exception):
            log.error(f"❌ {name} crashed: {r}")
        else:
            log.info(f"📦 {name}: {len(r)} jobs collected")
            all_jobs.extend(r)

    log.info(f"\n📦 TOTAL collected: {len(all_jobs)}")

    saved = skipped = failed = 0
    for job in all_jobs:
        try:
            result = await save_job(job)
            if result:
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"Pipeline error: {e}")
            failed += 1

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n{'='*55}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   ✅ Saved:   {saved}")
    log.info(f"   ⏭  Skipped: {skipped} (already in DB)")
    log.info(f"   ❌ Failed:  {failed}")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    asyncio.run(main())
