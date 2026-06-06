import os
import re
import asyncio
import hashlib
import logging
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import urllib.request
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
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
ADZUNA_APP_ID    = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY   = os.environ.get("ADZUNA_APP_KEY", "")
JOOBLE_API_KEY   = os.environ.get("JOOBLE_API_KEY", "")

TRANSLATE_ENABLED = bool(GEMINI_API_KEY)
ADZUNA_ENABLED    = bool(ADZUNA_APP_ID and ADZUNA_APP_KEY)
JOOBLE_ENABLED    = bool(JOOBLE_API_KEY)

if TRANSLATE_ENABLED:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel("gemini-1.5-flash")
    log.info("✅ Gemini translation ENABLED")
else:
    log.info("⏭  Gemini SKIPPED")

log.info(f"{'✅' if ADZUNA_ENABLED else '❌'} Adzuna API {'ENABLED' if ADZUNA_ENABLED else 'DISABLED'}")
log.info(f"{'✅' if JOOBLE_ENABLED else '❌'} Jooble API {'ENABLED' if JOOBLE_ENABLED else 'DISABLED'}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
MAX_PER_SOURCE = 50

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def make_key(platform: str, uid: str) -> str:
    return hashlib.sha256(f"{platform}::{uid}".encode()).hexdigest()


def clean_url(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if "linkedin.com" in parsed.netloc:
            m = re.search(r'/jobs/view/[^/?]+', parsed.path)
            if m:
                return f"https://www.linkedin.com{m.group(0)}"
        junk = {'trackingId','refId','pageNum','position','searchId','trk','src','sid'}
        qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in junk}
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url


def detect_work_mode(title: str, desc: str = "") -> str:
    t = (title + " " + desc).lower()
    if any(w in t for w in ['remote','100% remote','fully remote','work from home','wfh','télétravail','full remote']):
        return 'Remote'
    if any(w in t for w in ['hybrid','hybride','flexible location']):
        return 'Hybrid'
    return 'Onsite'


def detect_category(title: str) -> str:
    t = title.lower()
    cats = {
        'Technology':       ['developer','engineer','software','data','devops','cloud','cyber','programmer',
                             'fullstack','frontend','backend','mobile','architect','sysadmin','network',
                             'database','qa ','tester','it ','tech','machine learning','artificial intelligence'],
        'Sales':            ['sales','account manager','business development','bd ','revenue','commercial','pre-sales'],
        'Marketing':        ['marketing','seo','content','social media','brand','digital','media buyer',
                             'growth','acquisition','ppc','campaign','community manager'],
        'Finance':          ['finance','accounting','accountant','auditor','tax','treasury',
                             'financial analyst','cfo','comptable','budget','controller'],
        'HR':               ['hr ','human resources','talent','recruiter','recruitment','payroll','people ops','rh '],
        'Operations':       ['operations','logistics','supply chain','procurement','purchasing',
                             'warehouse','inventory','facilities','fleet'],
        'Healthcare':       ['doctor','nurse','pharmacist','medical','health','clinical',
                             'dentist','sage femme','midwife','radiology','laboratory'],
        'Education':        ['teacher','instructor','professor','tutor','trainer','educational','enseignant'],
        'Design':           ['designer','ux','ui ','graphic','creative','visual','illustrator','figma','motion'],
        'Customer Service': ['customer service','support','helpdesk','call center','client relations'],
        'Management':       ['manager','director','head of','chief','ceo','cto','coo',
                             'vp ','vice president','general manager'],
        'Engineering':      ['mechanical','electrical','civil','chemical','industrial',
                             'construction','maintenance','structural'],
        'Legal':            ['lawyer','legal','counsel','compliance','contract','paralegal','attorney','avocat'],
        'Admin':            ['assistant','secretary','receptionist','administrative','coordinator',
                             'office manager','réceptionniste'],
    }
    for cat, kws in cats.items():
        if any(k in t for k in kws):
            return cat
    return 'Other'


def already_exists(key: str) -> bool:
    r = supabase.table("jobs").select("job_key").eq("job_key", key).execute()
    return len(r.data) > 0


async def translate(text: str) -> str:
    if not TRANSLATE_ENABLED or not text.strip():
        return ""
    prompt = (
        "You are a professional HR translator for the Arab world. "
        "Translate into clear modern business Arabic for MENA job seekers. "
        "Preserve technical terms. Return ONLY the translated text.\n\n" + text.strip()
    )
    for attempt in range(1, 4):
        try:
            return ai_model.generate_content(prompt).text.strip()
        except Exception as err:
            log.warning(f"Translation attempt {attempt}/3: {err}")
            await asyncio.sleep(2 ** attempt)
    return ""


async def save_job(job: dict) -> bool:
    if already_exists(job["job_key"]):
        log.info(f"  ⏭  Skip: {job['title_en'][:50]}")
        return False
    if TRANSLATE_ENABLED:
        job["title_ar"]       = await translate(job["title_en"])
        job["description_ar"] = await translate(job.get("description_en", ""))
        job["translation_status"] = "completed" if job["title_ar"] else "failed"
    else:
        job["title_ar"] = job["description_ar"] = ""
        job["translation_status"] = "pending"
    try:
        supabase.table("jobs").insert(job).execute()
        log.info(f"  ✅ [{job['source_platform']:14}][{job['country']}][{job['job_category']:14}][{job['work_mode']:7}] {job['title_en'][:38]}")
        return True
    except Exception as err:
        log.error(f"  ❌ DB: {err} — {job['title_en'][:40]}")
        return False


def build_job(platform, uid, title, company, country, url, description="") -> dict:
    c = clean_url(url)
    return {
        "job_key":            make_key(platform.lower(), uid),
        "title_en":           title.strip(),
        "company_name":       (company or "Unknown").strip(),
        "description_en":     description.strip() if description else f"Full details at {c}",
        "title_ar":           "",
        "description_ar":     "",
        "translation_status": "pending",
        "country":            country,
        "location_city":      "",
        "work_mode":          detect_work_mode(title, description),
        "job_category":       detect_category(title),
        "salary_range":       "",
        "source_url":         c,
        "source_platform":    platform,
        "is_active":          True,
        "posted_at":          datetime.now(timezone.utc).isoformat(),
    }


def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  API SOURCE 1 — Adzuna (official REST API)
#  Docs: https://developer.adzuna.com/
#  Free tier: 250 req/month — we use ~20 per run
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_adzuna() -> list[dict]:
    if not ADZUNA_ENABLED:
        log.warning("⏭  Adzuna skipped — no credentials")
        return []

    jobs = []

    # Adzuna country codes for MENA
    # ae=UAE, sa=Saudi, eg=Egypt, ma=Morocco, ng≈closest for other Arab
    targets = [
        ("AE", "ae", ""),          # UAE — all jobs
        ("AE", "ae", "developer"), # UAE — tech focus
        ("AE", "ae", "manager"),   # UAE — management
        ("SA", "gb", "saudi arabia"),  # Adzuna doesn't have SA — use GB with location filter
        ("EG", "za", "egypt"),     # Closest available country code with Egypt jobs
        ("MA", "gb", "morocco"),   # Morocco jobs via GB search
    ]

    # Actual Adzuna supported MENA-adjacent countries
    adzuna_countries = [
        ("AE", "ae"),   # United Arab Emirates ✅ supported
        ("GB_SA", "gb"), # Saudi-related jobs on UK board
        ("IN_EG", "in"), # Egypt-related on India board (large market)
        ("ZA_MA", "za"), # Morocco-related on South Africa board
    ]

    # Best approach: use supported countries directly
    direct_targets = [
        ("AE", "ae", 1),
        ("AE", "ae", 2),
        ("AE", "ae", 3),
    ]

    for country, adzuna_cc, page in direct_targets:
        url = (
            f"https://api.adzuna.com/v1/api/jobs/{adzuna_cc}/search/{page}"
            f"?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}"
            f"&results_per_page=50&content-type=application/json"
            f"&sort_by=date"
        )
        log.info(f"🔌 Adzuna API → {country} (page {page})")
        try:
            data = http_get_json(url)
            results = data.get("results", [])
            log.info(f"   {len(results)} jobs returned")
            for job in results:
                try:
                    title     = job.get("title", "").strip()
                    company   = job.get("company", {}).get("display_name", "Unknown")
                    location  = job.get("location", {}).get("display_name", "")
                    desc      = re.sub(r'<[^>]+>', ' ', job.get("description", "")).strip()[:400]
                    link      = job.get("redirect_url", "")
                    job_id    = str(job.get("id", title[:30]))
                    salary_min = job.get("salary_min")
                    salary_max = job.get("salary_max")
                    salary    = f"{salary_min:.0f}-{salary_max:.0f} AED" if salary_min and salary_max else ""

                    if not title:
                        continue

                    j = build_job("Adzuna", job_id, title, company, country, link, desc)
                    j["location_city"] = location
                    j["salary_range"]  = salary
                    jobs.append(j)
                except Exception as e:
                    log.debug(f"adzuna item err: {e}")
        except Exception as e:
            log.warning(f"   Adzuna error ({country} p{page}): {e}")

    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  API SOURCE 2 — Jooble (official REST API)
#  Docs: https://jooble.org/api/about
#  500 req/month free — we use ~12 per run
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_jooble() -> list[dict]:
    if not JOOBLE_ENABLED:
        log.warning("⏭  Jooble skipped — no API key")
        return []

    jobs = []
    base_url = f"https://jooble.org/api/{JOOBLE_API_KEY}"

    # Jooble supports location as free text — great for MENA
    searches = [
        ("AE", {"keywords": "",            "location": "United Arab Emirates", "page": 1}),
        ("AE", {"keywords": "developer",   "location": "Dubai",                "page": 1}),
        ("AE", {"keywords": "manager",     "location": "Abu Dhabi",            "page": 1}),
        ("SA", {"keywords": "",            "location": "Saudi Arabia",         "page": 1}),
        ("SA", {"keywords": "engineer",    "location": "Riyadh",               "page": 1}),
        ("SA", {"keywords": "sales",       "location": "Jeddah",               "page": 1}),
        ("EG", {"keywords": "",            "location": "Egypt",                "page": 1}),
        ("EG", {"keywords": "developer",   "location": "Cairo",                "page": 1}),
        ("MA", {"keywords": "",            "location": "Morocco",              "page": 1}),
        ("MA", {"keywords": "ingenieur",   "location": "Casablanca",           "page": 1}),
        ("QA", {"keywords": "",            "location": "Qatar",                "page": 1}),
        ("KW", {"keywords": "",            "location": "Kuwait",               "page": 1}),
        ("TN", {"keywords": "",            "location": "Tunisia",              "page": 1}),
        ("DZ", {"keywords": "",            "location": "Algeria",              "page": 1}),
        ("JO", {"keywords": "",            "location": "Jordan",               "page": 1}),
        ("LB", {"keywords": "",            "location": "Lebanon",              "page": 1}),
    ]

    for country, payload in searches:
        log.info(f"🔌 Jooble API → {country} [{payload.get('keywords','all')}] {payload['location']}")
        try:
            data = http_post_json(base_url, payload)
            results = data.get("jobs", [])
            log.info(f"   {len(results)} jobs returned")
            for job in results:
                try:
                    title   = job.get("title", "").strip()
                    company = job.get("company", "Unknown").strip()
                    link    = job.get("link", "")
                    desc    = re.sub(r'<[^>]+>', ' ', job.get("snippet", "")).strip()[:400]
                    salary  = job.get("salary", "")
                    loc     = job.get("location", "")
                    job_id  = job.get("id", "") or make_key("jooble_raw", title + loc)

                    if not title:
                        continue

                    j = build_job("Jooble", str(job_id), title, company, country, link, desc)
                    j["location_city"] = loc
                    j["salary_range"]  = salary
                    jobs.append(j)
                except Exception as e:
                    log.debug(f"jooble item err: {e}")

            await asyncio.sleep(0.3)  # gentle rate limiting
        except Exception as e:
            log.warning(f"   Jooble error ({country}): {e}")

    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER SOURCE 1 — LinkedIn (working perfectly)
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
        ("TN", "https://www.linkedin.com/jobs/search/?location=Tunisia&f_TPR=r86400&start=0"),
        ("DZ", "https://www.linkedin.com/jobs/search/?location=Algeria&f_TPR=r86400&start=0"),
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
                log.info(f"   {len(cards)} cards")
                for card in cards[:MAX_PER_SOURCE]:
                    try:
                        title   = (await card.locator(".base-search-card__title").inner_text()).strip()
                        company = (await card.locator(".base-search-card__subtitle").inner_text()).strip()
                        href    = await card.locator("a.base-card__full-link").get_attribute("href") or ""
                        m       = re.search(r'/jobs/view/(\d+)', href)
                        uid     = m.group(1) if m else href[-20:]
                        if title:
                            jobs.append(build_job("LinkedIn", uid, title, company, country, href))
                    except Exception as e:
                        log.debug(f"linkedin card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: LinkedIn {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER SOURCE 2 — Wuzzuf (working)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_wuzzuf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        log.info("🌐 Wuzzuf → EG")
        await page.goto("https://wuzzuf.net/search/jobs/?q=&a=hpb", wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector("h2 a[href*='/jobs/p/']", timeout=15000)
        except PlaywrightTimeout:
            log.warning("   Wuzzuf: selector wait timed out")

        links = await page.locator("h2 a[href*='/jobs/p/']").all()
        log.info(f"   {len(links)} job links")
        for link in links[:MAX_PER_SOURCE]:
            try:
                title  = (await link.inner_text()).strip()
                href   = await link.get_attribute("href") or ""
                full   = f"https://wuzzuf.net{href}" if not href.startswith("http") else href
                parent = link.locator("xpath=../../../..")
                company = ""
                try:
                    company = (await parent.locator("a[href*='/company/']").first.inner_text()).strip()
                except Exception:
                    pass
                uid = re.sub(r'\?.*', '', href).split("/")[-1]
                if title:
                    jobs.append(build_job("Wuzzuf", uid, title, company, "EG", full))
            except Exception as e:
                log.debug(f"wuzzuf err: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Wuzzuf")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA Jobs Scraper v4 — starting")
    start = datetime.now()

    # ── Phase 1: Official APIs (fast, reliable, no blocking) ──────────────────
    log.info("\n── Phase 1: Official APIs ──")
    adzuna_jobs = await fetch_adzuna()
    jooble_jobs = await fetch_jooble()

    # ── Phase 2: Browser scrapers ─────────────────────────────────────────────
    log.info("\n── Phase 2: Browser scrapers ──")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        linkedin_jobs, wuzzuf_jobs = await asyncio.gather(
            scrape_linkedin(ctx),
            scrape_wuzzuf(ctx),
            return_exceptions=True
        )
        await browser.close()

    # ── Flatten + report ───────────────────────────────────────────────────────
    all_jobs: list[dict] = []
    sources = {
        "Adzuna API":  adzuna_jobs,
        "Jooble API":  jooble_jobs,
        "LinkedIn":    linkedin_jobs,
        "Wuzzuf":      wuzzuf_jobs,
    }
    for name, result in sources.items():
        if isinstance(result, Exception):
            log.error(f"❌ {name} crashed: {result}")
        elif isinstance(result, list):
            log.info(f"📦 {name}: {len(result)} jobs")
            all_jobs.extend(result)

    log.info(f"\n📦 TOTAL collected: {len(all_jobs)}")

    # ── Save all ───────────────────────────────────────────────────────────────
    saved = skipped = failed = 0
    for job in all_jobs:
        try:
            if await save_job(job):
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"Pipeline err: {e}")
            failed += 1

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n{'='*60}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   ✅ Saved:   {saved}")
    log.info(f"   ⏭  Skipped: {skipped} (already in DB)")
    log.info(f"   ❌ Failed:  {failed}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
