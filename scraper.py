import os
import re
import asyncio
import hashlib
import logging
import json
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
SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_KEY        = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
ADZUNA_APP_ID       = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY      = os.environ.get("ADZUNA_APP_KEY", "")
JOOBLE_API_KEY      = os.environ.get("JOOBLE_API_KEY", "")

TRANSLATE_ENABLED   = bool(OPENROUTER_API_KEY)
ADZUNA_ENABLED      = bool(ADZUNA_APP_ID and ADZUNA_APP_KEY)
JOOBLE_ENABLED      = bool(JOOBLE_API_KEY)

# Best model for Arabic: native Arabic training, cheap, fast
# $0.30/M input + $1.80/M output — perfect for short job titles
TRANSLATE_MODEL     = "qwen/qwen2.5-72b-instruct"
TRANSLATE_BATCH     = 15   # titles per API call — reduces cost by 15x
TRANSLATE_MAX_RETRY = 3

log.info(f"{'✅' if TRANSLATE_ENABLED  else '⏭ '} OpenRouter translation {'ENABLED — ' + TRANSLATE_MODEL if TRANSLATE_ENABLED else 'SKIPPED'}")
log.info(f"{'✅' if ADZUNA_ENABLED     else '❌'} Adzuna  {'ENABLED' if ADZUNA_ENABLED  else 'DISABLED'}")
log.info(f"{'✅' if JOOBLE_ENABLED     else '❌'} Jooble  {'ENABLED' if JOOBLE_ENABLED  else 'DISABLED'}")

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
        qs   = {k: v for k, v in parse_qs(parsed.query).items() if k not in junk}
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url


def detect_work_mode(title: str, desc: str = "") -> str:
    t = (title + " " + desc).lower()
    if any(w in t for w in ['remote','100% remote','fully remote','work from home','wfh','télétravail']):
        return 'Remote'
    if any(w in t for w in ['hybrid','hybride','flexible location']):
        return 'Hybrid'
    return 'Onsite'


def detect_category(title: str) -> str:
    t = title.lower()
    cats = {
        'Technology':       ['developer','engineer','software','data','devops','cloud','cyber',
                             'programmer','fullstack','frontend','backend','mobile','architect',
                             'sysadmin','network','database','qa ','tester','it ','machine learning'],
        'Sales':            ['sales','account manager','business development','bd ','commercial','pre-sales'],
        'Marketing':        ['marketing','seo','content','social media','brand','digital',
                             'media buyer','growth','ppc','community manager'],
        'Finance':          ['finance','accounting','accountant','auditor','tax','treasury',
                             'financial','cfo','comptable','budget','controller'],
        'HR':               ['hr ','human resources','talent','recruiter','recruitment','payroll','rh '],
        'Operations':       ['operations','logistics','supply chain','procurement',
                             'warehouse','inventory','facilities','fleet'],
        'Healthcare':       ['doctor','nurse','pharmacist','medical','health','clinical',
                             'dentist','sage femme','midwife','radiology'],
        'Education':        ['teacher','instructor','professor','tutor','trainer','enseignant'],
        'Design':           ['designer','ux','ui ','graphic','creative','visual','figma','motion'],
        'Customer Service': ['customer service','support','helpdesk','call center','client relations'],
        'Management':       ['manager','director','head of','chief','ceo','cto','coo',
                             'vp ','vice president','general manager'],
        'Engineering':      ['mechanical','electrical','civil','chemical','industrial',
                             'construction','maintenance','structural'],
        'Legal':            ['lawyer','legal','counsel','compliance','contract','paralegal','avocat'],
        'Admin':            ['assistant','secretary','receptionist','administrative',
                             'coordinator','office manager'],
    }
    for cat, kws in cats.items():
        if any(k in t for k in kws):
            return cat
    return 'Other'


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
        "Accept":     "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, headers: dict = None, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH TRANSLATION — OpenRouter (ultra low cost)
#  Strategy:
#    • Translate ONLY title_en (8-12 words avg) — not description
#    • Send 15 titles per API call (15x cheaper than 1 per call)
#    • Skip any job where title_ar is already filled
#    • Model: qwen/qwen2.5-32b-instruct — best Arabic quality at lowest price
# ══════════════════════════════════════════════════════════════════════════════

async def batch_translate(titles: list[str]) -> list[str]:
    """
    Translate a list of job titles to Arabic in ONE API call.
    Returns a list of Arabic strings in the same order.
    Falls back to empty strings on any error.
    """
    if not TRANSLATE_ENABLED or not titles:
        return [""] * len(titles)

    # Build a numbered list prompt — model returns numbered Arabic list
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "You are a professional HR translator. "
        "Translate each numbered job title below into professional Arabic. "
        "Return ONLY a numbered list in the exact same order. "
        "No explanations. No extra text. Example format:\n"
        "1. مطور برمجيات\n"
        "2. مدير مبيعات\n\n"
        f"{numbered}"
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 600,
        "temperature": 0.1,   # low temp = consistent translations
    }
    headers = {
        "Authorization":  f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer":   "https://github.com/mena-jobs-scraper",
        "X-Title":        "MENA Jobs Scraper",
    }

    for attempt in range(1, TRANSLATE_MAX_RETRY + 1):
        try:
            resp = http_post_json(
                "https://openrouter.ai/api/v1/chat/completions",
                payload, headers
            )
            raw = resp["choices"][0]["message"]["content"].strip()

            # Parse numbered list back to array
            results = [""] * len(titles)
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(\d+)[.\)]\s*(.+)$', line)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(titles):
                        results[idx] = m.group(2).strip()

            # Validate — if we got back less than 50% fill, retry
            filled = sum(1 for r in results if r)
            if filled < len(titles) * 0.5:
                log.warning(f"   Batch parse low quality ({filled}/{len(titles)}) attempt {attempt}, retrying")
                await asyncio.sleep(2)
                continue

            log.info(f"   ✅ Batch translated {filled}/{len(titles)} titles")
            return results

        except Exception as e:
            log.warning(f"   Translation attempt {attempt}/{TRANSLATE_MAX_RETRY}: {e}")
            await asyncio.sleep(2 ** attempt)

    log.error("   ❌ All translation attempts failed, saving as pending")
    return [""] * len(titles)


async def translate_and_save_batch(new_jobs: list[dict]) -> tuple[int, int]:
    """
    Takes a list of NEW jobs (not in DB yet).
    Batch-translates their titles 15 at a time.
    Inserts all into Supabase.
    Returns (saved_count, failed_count)
    """
    if not new_jobs:
        return 0, 0

    saved = failed = 0
    batch_size = TRANSLATE_BATCH

    for i in range(0, len(new_jobs), batch_size):
        batch = new_jobs[i:i + batch_size]
        titles = [j["title_en"] for j in batch]

        log.info(f"\n  🤖 Translating batch {i//batch_size + 1} ({len(titles)} titles)...")
        arabic_titles = await batch_translate(titles)

        for job, title_ar in zip(batch, arabic_titles):
            job["title_ar"]           = title_ar
            job["description_ar"]     = ""          # skip desc translation — saves 70% cost
            job["translation_status"] = "completed" if title_ar else "pending"
            try:
                supabase.table("jobs").insert(job).execute()
                log.info(f"    ✅ [{job['source_platform']:14}][{job['country']}][{job['job_category']:14}] {job['title_en'][:35]} → {title_ar[:30]}")
                saved += 1
            except Exception as e:
                log.error(f"    ❌ DB insert: {e} — {job['title_en'][:40]}")
                failed += 1

        # Small pause between batches to respect rate limits
        if i + batch_size < len(new_jobs):
            await asyncio.sleep(1)

    return saved, failed


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_existing_keys(keys: list[str]) -> set[str]:
    """
    Batch-check which job_keys already exist in DB.
    One query for the whole list — much faster than 1 query per job.
    """
    if not keys:
        return set()
    try:
        result = supabase.table("jobs").select("job_key").in_("job_key", keys).execute()
        return {row["job_key"] for row in result.data}
    except Exception as e:
        log.error(f"DB key lookup error: {e}")
        return set()


def filter_new_jobs(jobs: list[dict]) -> list[dict]:
    """Remove jobs already in DB using a single batch query."""
    if not jobs:
        return []
    keys = [j["job_key"] for j in jobs]
    existing = get_existing_keys(keys)
    new = [j for j in jobs if j["job_key"] not in existing]
    skipped = len(jobs) - len(new)
    if skipped:
        log.info(f"  ⏭  {skipped} already in DB, {len(new)} new")
    return new


# ══════════════════════════════════════════════════════════════════════════════
#  API SOURCE 1 — Adzuna
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_adzuna() -> list[dict]:
    if not ADZUNA_ENABLED:
        log.info("⏭  Adzuna skipped")
        return []
    jobs = []
    targets = [
        ("AE", "ae", 1), ("AE", "ae", 2), ("AE", "ae", 3),
    ]
    for country, cc, page in targets:
        url = (
            f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"
            f"?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}"
            f"&results_per_page=50&content-type=application/json&sort_by=date"
        )
        log.info(f"🔌 Adzuna → {country} p{page}")
        try:
            data    = http_get_json(url)
            results = data.get("results", [])
            log.info(f"   {len(results)} jobs")
            for job in results:
                title   = job.get("title", "").strip()
                company = job.get("company", {}).get("display_name", "Unknown")
                loc     = job.get("location", {}).get("display_name", "")
                desc    = re.sub(r'<[^>]+>', ' ', job.get("description", "")).strip()[:300]
                link    = job.get("redirect_url", "")
                jid     = str(job.get("id", title[:30]))
                sal_min = job.get("salary_min")
                sal_max = job.get("salary_max")
                salary  = f"{sal_min:.0f}-{sal_max:.0f} AED" if sal_min and sal_max else ""
                if not title:
                    continue
                j = build_job("Adzuna", jid, title, company, country, link, desc)
                j["location_city"] = loc
                j["salary_range"]  = salary
                jobs.append(j)
        except Exception as e:
            log.warning(f"   Adzuna error: {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  API SOURCE 2 — Jooble
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_jooble() -> list[dict]:
    if not JOOBLE_ENABLED:
        log.info("⏭  Jooble skipped")
        return []
    jobs = []
    base = f"https://jooble.org/api/{JOOBLE_API_KEY}"
    searches = [
        ("AE", {"keywords": "",           "location": "United Arab Emirates", "page": 1}),
        ("AE", {"keywords": "developer",  "location": "Dubai",                "page": 1}),
        ("SA", {"keywords": "",           "location": "Saudi Arabia",         "page": 1}),
        ("SA", {"keywords": "engineer",   "location": "Riyadh",               "page": 1}),
        ("EG", {"keywords": "",           "location": "Egypt",                "page": 1}),
        ("EG", {"keywords": "developer",  "location": "Cairo",                "page": 1}),
        ("MA", {"keywords": "",           "location": "Morocco",              "page": 1}),
        ("QA", {"keywords": "",           "location": "Qatar",                "page": 1}),
        ("KW", {"keywords": "",           "location": "Kuwait",               "page": 1}),
        ("TN", {"keywords": "",           "location": "Tunisia",              "page": 1}),
        ("DZ", {"keywords": "",           "location": "Algeria",              "page": 1}),
        ("JO", {"keywords": "",           "location": "Jordan",               "page": 1}),
    ]
    for country, payload in searches:
        log.info(f"🔌 Jooble → {country} [{payload.get('keywords','all')}]")
        try:
            data    = http_post_json(base, payload)
            results = data.get("jobs", [])
            log.info(f"   {len(results)} jobs")
            for job in results:
                title   = job.get("title", "").strip()
                company = job.get("company", "Unknown").strip()
                link    = job.get("link", "")
                desc    = re.sub(r'<[^>]+>', ' ', job.get("snippet", "")).strip()[:300]
                salary  = job.get("salary", "")
                loc     = job.get("location", "")
                jid     = str(job.get("id", "") or make_key("jooble_raw", title + loc))
                if not title:
                    continue
                j = build_job("Jooble", jid, title, company, country, link, desc)
                j["location_city"] = loc
                j["salary_range"]  = salary
                jobs.append(j)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning(f"   Jooble error ({country}): {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER — LinkedIn
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
#  BROWSER — Wuzzuf
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_wuzzuf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        log.info("🌐 Wuzzuf → EG")
        await page.goto("https://wuzzuf.net/search/jobs/?q=&a=hpb", wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector("h2 a[href*='/jobs/p/']", timeout=15000)
        except PlaywrightTimeout:
            pass
        links = await page.locator("h2 a[href*='/jobs/p/']").all()
        log.info(f"   {len(links)} links")
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
    log.info("🚀 MENA Jobs Scraper v5 — starting")
    start = datetime.now()

    # ── Phase 1: APIs ─────────────────────────────────────────────────────────
    log.info("\n── Phase 1: APIs ──")
    adzuna_jobs, jooble_jobs = await asyncio.gather(
        fetch_adzuna(),
        fetch_jooble(),
        return_exceptions=True
    )

    # ── Phase 2: Browser ──────────────────────────────────────────────────────
    log.info("\n── Phase 2: Browser ──")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(
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

    # ── Flatten ───────────────────────────────────────────────────────────────
    all_jobs: list[dict] = []
    for name, result in [("Adzuna", adzuna_jobs), ("Jooble", jooble_jobs),
                          ("LinkedIn", linkedin_jobs), ("Wuzzuf", wuzzuf_jobs)]:
        if isinstance(result, Exception):
            log.error(f"❌ {name} crashed: {result}")
        else:
            log.info(f"📦 {name}: {len(result)} collected")
            all_jobs.extend(result)

    log.info(f"\n📦 TOTAL collected: {len(all_jobs)}")

    # ── Phase 3: Dedup — one batch DB query ───────────────────────────────────
    log.info("\n── Phase 3: Dedup ──")
    new_jobs = filter_new_jobs(all_jobs)
    log.info(f"🆕 New jobs to save: {len(new_jobs)}")

    # ── Phase 4: Batch translate + save ───────────────────────────────────────
    log.info("\n── Phase 4: Translate & Save ──")
    if new_jobs:
        saved, failed = await translate_and_save_batch(new_jobs)
    else:
        saved = failed = 0

    elapsed = (datetime.now() - start).seconds
    log.info(f"\n{'='*60}")
    log.info(f"🏁 Done in {elapsed}s")
    log.info(f"   📦 Collected: {len(all_jobs)}")
    log.info(f"   🆕 New:       {len(new_jobs)}")
    log.info(f"   ✅ Saved:     {saved}")
    log.info(f"   ❌ Failed:    {failed}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
