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
MAX_PER_SOURCE = 40

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
        'Technology':       ['developer','engineer','software','data','devops','cloud','cyber','programmer','fullstack','frontend','backend','mobile','architect','sysadmin','network','database','qa ','test'],
        'Sales':            ['sales','account manager','business development','bd ','revenue','commercial','pre-sales'],
        'Marketing':        ['marketing','seo','content','social media','brand','digital','media buyer','growth','acquisition','ppc','campaign'],
        'Finance':          ['finance','accounting','accountant','auditor','tax','treasury','financial analyst','cfo','comptable','budget'],
        'HR':               ['hr ','human resources','talent','recruiter','recruitment','payroll','people ops','rh '],
        'Operations':       ['operations','logistics','supply chain','procurement','purchasing','warehouse','inventory','facilities','fleet'],
        'Healthcare':       ['doctor','nurse','pharmacist','medical','health','clinical','dentist','sage femme','midwife','radiology','laboratory'],
        'Education':        ['teacher','instructor','professor','tutor','trainer','educational','enseignant','formateur'],
        'Design':           ['designer','ux','ui ','graphic','creative','visual','illustrator','figma','motion'],
        'Customer Service': ['customer service','support','helpdesk','call center','client relations','after sales'],
        'Management':       ['manager','director','head of','chief','ceo','cto','coo','vp ','vice president','general manager'],
        'Engineering':      ['mechanical','electrical','civil','chemical','industrial','construction','maintenance','structural','geotechnical'],
        'Legal':            ['lawyer','legal','counsel','compliance','contract','paralegal','attorney','avocat','juriste'],
        'Admin':            ['assistant','secretary','receptionist','administrative','coordinator','office manager','réceptionniste'],
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
        log.info(f"  ⏭  Skip: {job['title_en'][:55]}")
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
        log.info(f"  ✅ [{job['source_platform']:14}][{job['country']}][{job['job_category']:14}][{job['work_mode']:7}] {job['title_en'][:40]}")
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


def http_get(url: str, timeout: int = 15) -> str:
    """Simple HTTP GET that works inside GitHub Actions."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 1 — LinkedIn (Playwright, WORKING — keep as-is)
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
                        log.debug(f"card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout LinkedIn {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 2 — Wuzzuf (Playwright + wait for React render)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_wuzzuf(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://wuzzuf.net/search/jobs/?q=&a=hpb"
        log.info("🌐 Wuzzuf → EG")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Wait for React to hydrate — look for any h2 inside a job card
        try:
            await page.wait_for_selector("h2 a[href*='/jobs/p/']", timeout=15000)
        except PlaywrightTimeout:
            log.warning("   Wuzzuf: selector wait timed out, trying anyway")

        # Dump DOM and find all job links
        links = await page.locator("h2 a[href*='/jobs/p/']").all()
        log.info(f"   {len(links)} job links found")
        for link in links[:MAX_PER_SOURCE]:
            try:
                title = (await link.inner_text()).strip()
                href  = await link.get_attribute("href") or ""
                full  = f"https://wuzzuf.net{href}" if not href.startswith("http") else href
                # company is the next sibling anchor usually
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
                log.debug(f"wuzzuf link err: {e}")
    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Wuzzuf")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 3 — Bayt (Playwright + wait for job list)
# ══════════════════════════════════════════════════════════════════════════════

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
                # Wait for job items to appear
                try:
                    await page.wait_for_selector("li[data-job-id]", timeout=12000)
                except PlaywrightTimeout:
                    pass

                # Try data-job-id first (most reliable)
                cards = await page.locator("li[data-job-id]").all()

                # Fallback: find all job title links
                if not cards:
                    links = await page.locator("h2.jb-title a, a[href*='/job-details/']").all()
                    log.info(f"   fallback: {len(links)} links")
                    for link in links[:MAX_PER_SOURCE]:
                        try:
                            title = (await link.inner_text()).strip()
                            href  = await link.get_attribute("href") or ""
                            full  = href if href.startswith("http") else f"https://www.bayt.com{href}"
                            uid   = href.split("/")[-2] or title[:30]
                            if title:
                                jobs.append(build_job("Bayt", uid, title, "", country, full))
                        except Exception as e:
                            log.debug(f"bayt link err: {e}")
                    continue

                log.info(f"   {len(cards)} cards")
                for card in cards[:MAX_PER_SOURCE]:
                    try:
                        title_el = card.locator("h2.jb-title a, h2 a").first
                        title    = (await title_el.inner_text()).strip()
                        href     = await title_el.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.bayt.com{href}"
                        company  = ""
                        try:
                            company = (await card.locator("[class*='jb-company'], [class*='company']").first.inner_text()).strip()
                        except Exception:
                            pass
                        uid = href.split("/")[-2] or title[:30]
                        if title:
                            jobs.append(build_job("Bayt", uid, title, company, country, full_url))
                    except Exception as e:
                        log.debug(f"bayt card err: {e}")
            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Bayt {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 4 — Tanqeeb (Playwright + dump all links as fallback)
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

                # Dump page HTML to find actual selectors
                html = await page.content()
                # Log class names containing 'job' to help debug
                classes = set(re.findall(r'class="([^"]*job[^"]*)"', html, re.IGNORECASE))
                if classes:
                    log.info(f"   Job-related classes: {list(classes)[:8]}")

                # Try all possible selectors
                found = False
                for sel in ["div.job-card", "div[class*='job-card']", "article[class*='job']",
                            "div[class*='JobCard']", "div[class*='vacancy']", ".job-listing",
                            "div[data-job]", "li[class*='job']", "div[class*='job_item']",
                            "div[class*='JobItem']", "div[class*='job-item']"]:
                    cards = await page.locator(sel).all()
                    if cards:
                        log.info(f"   ✓ Matched: {sel} ({len(cards)} cards)")
                        for card in cards[:MAX_PER_SOURCE]:
                            try:
                                title_el = card.locator("h2, h3, [class*='title'], [class*='Title']").first
                                title    = (await title_el.inner_text()).strip()
                                href     = ""
                                try:
                                    href = await card.locator("a").first.get_attribute("href") or ""
                                except Exception:
                                    pass
                                full_url = href if href.startswith("http") else f"https://www.tanqeeb.com{href}"
                                company  = ""
                                try:
                                    company = (await card.locator("[class*='company'],[class*='employer']").first.inner_text()).strip()
                                except Exception:
                                    pass
                                uid = re.sub(r'\?.*', '', href).split("/")[-1] or title[:30]
                                if title:
                                    jobs.append(build_job("Tanqeeb", uid, title, company, country, full_url))
                            except Exception as e:
                                log.debug(f"tanqeeb card err: {e}")
                        found = True
                        break

                if not found:
                    # Last resort: grab all job-like links from page
                    links = await page.locator("a[href*='/job/'], a[href*='/jobs/'], a[href*='/vacancy/']").all()
                    log.info(f"   fallback links: {len(links)}")
                    seen_hrefs = set()
                    for link in links[:MAX_PER_SOURCE]:
                        try:
                            title = (await link.inner_text()).strip()
                            href  = await link.get_attribute("href") or ""
                            if not title or href in seen_hrefs or len(title) < 4:
                                continue
                            seen_hrefs.add(href)
                            full_url = href if href.startswith("http") else f"https://www.tanqeeb.com{href}"
                            uid = re.sub(r'\?.*', '', href).split("/")[-1] or title[:30]
                            jobs.append(build_job("Tanqeeb", uid, title, "", country, full_url))
                        except Exception as e:
                            log.debug(f"tanqeeb fallback err: {e}")

            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Tanqeeb {country}")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 5 — Dreamjob.ma (Playwright + WordPress fallback)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_dreamjob(ctx) -> list[dict]:
    jobs, page = [], await ctx.new_page()
    try:
        url = "https://www.dreamjob.ma/offres-emploi/"
        log.info("🌐 Dreamjob.ma → MA")
        await page.goto(url, wait_until="networkidle", timeout=50000)
        await page.wait_for_timeout(4000)

        html = await page.content()
        classes = set(re.findall(r'class="([^"]*job[^"]*)"', html, re.IGNORECASE))
        if classes:
            log.info(f"   Job classes: {list(classes)[:8]}")

        found = False
        for sel in ["li.job_listing", "div.job_listing", "[class*='job_listing']",
                    "article[class*='job']", ".jobList li", "div.job-item",
                    "li[class*='job']", "div[class*='offre']", "article.offre"]:
            cards = await page.locator(sel).all()
            if cards:
                log.info(f"   ✓ Matched: {sel} ({len(cards)} cards)")
                for card in cards[:MAX_PER_SOURCE]:
                    try:
                        title_el = card.locator("h3 a, h2 a, a[class*='title'], .position a").first
                        title    = (await title_el.inner_text()).strip()
                        href     = await title_el.get_attribute("href") or ""
                        full_url = href if href.startswith("http") else f"https://www.dreamjob.ma{href}"
                        company  = ""
                        try:
                            company = (await card.locator(".company, strong, [class*='company']").first.inner_text()).strip()
                        except Exception:
                            pass
                        uid = re.sub(r'\?.*', '', href).split("/")[-2] or title[:30]
                        if title:
                            jobs.append(build_job("Dreamjob.ma", uid, title, company, "MA", full_url))
                    except Exception as e:
                        log.debug(f"dreamjob card err: {e}")
                found = True
                break

        if not found:
            links = await page.locator("a[href*='/offre-'], a[href*='/emploi/'], a[href*='/job/']").all()
            log.info(f"   fallback links: {len(links)}")
            seen = set()
            for link in links[:MAX_PER_SOURCE]:
                try:
                    title = (await link.inner_text()).strip()
                    href  = await link.get_attribute("href") or ""
                    if not title or href in seen or len(title) < 4:
                        continue
                    seen.add(href)
                    full_url = href if href.startswith("http") else f"https://www.dreamjob.ma{href}"
                    uid = re.sub(r'\?.*', '', href).split("/")[-2] or title[:30]
                    jobs.append(build_job("Dreamjob.ma", uid, title, "", "MA", full_url))
                except Exception as e:
                    log.debug(f"dreamjob fallback err: {e}")

    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Dreamjob.ma")
    finally:
        await page.close()
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 6 — Rekrute.com RSS feed (Morocco — no scraping needed!)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_rekrute_rss() -> list[dict]:
    """Rekrute.com exposes a public RSS feed — zero CSS selectors needed."""
    jobs = []
    feeds = [
        ("MA", "https://www.rekrute.com/offres.rss?s=1&p=1&o=1"),
        ("MA", "https://www.rekrute.com/offres.rss?s=1&p=2&o=1"),
        ("MA", "https://www.rekrute.com/offres.rss?s=1&p=3&o=1"),
    ]
    for country, url in feeds:
        log.info(f"📡 Rekrute RSS → {country}")
        try:
            xml_text = http_get(url)
            root = ET.fromstring(xml_text)
            items = root.findall(".//item")
            log.info(f"   {len(items)} items in feed")
            for item in items[:MAX_PER_SOURCE]:
                try:
                    title   = (item.findtext("title") or "").strip()
                    link    = (item.findtext("link") or "").strip()
                    desc    = (item.findtext("description") or "").strip()
                    # Strip HTML tags from description
                    desc_clean = re.sub(r'<[^>]+>', ' ', desc).strip()[:300]
                    uid = re.sub(r'\?.*', '', link).split("/")[-1] or title[:30]
                    company = ""
                    # Company often in title as "Title - Company"
                    if " - " in title:
                        parts = title.rsplit(" - ", 1)
                        title, company = parts[0].strip(), parts[1].strip()
                    if title:
                        jobs.append(build_job("Rekrute", uid, title, company, country, link, desc_clean))
                except Exception as e:
                    log.debug(f"rekrute item err: {e}")
        except Exception as e:
            log.warning(f"   Rekrute RSS error: {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 7 — Emploi.ma RSS feed (Morocco)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_emploima_rss() -> list[dict]:
    jobs = []
    feeds = [
        ("MA", "https://www.emploi.ma/rss/offres-emploi.rss"),
        ("MA", "https://www.emploi.ma/rss/offres-emploi.rss?page=2"),
    ]
    for country, url in feeds:
        log.info(f"📡 Emploi.ma RSS → {country}")
        try:
            xml_text = http_get(url)
            root = ET.fromstring(xml_text)
            items = root.findall(".//item")
            log.info(f"   {len(items)} items")
            for item in items[:MAX_PER_SOURCE]:
                try:
                    title   = (item.findtext("title") or "").strip()
                    link    = (item.findtext("link") or "").strip()
                    desc    = re.sub(r'<[^>]+>', ' ', item.findtext("description") or "").strip()[:300]
                    uid     = re.sub(r'\?.*', '', link).split("/")[-1] or title[:30]
                    company = ""
                    if " - " in title:
                        parts = title.rsplit(" - ", 1)
                        title, company = parts[0].strip(), parts[1].strip()
                    if title:
                        jobs.append(build_job("Emploi.ma", uid, title, company, country, link, desc))
                except Exception as e:
                    log.debug(f"emploi.ma item err: {e}")
        except Exception as e:
            log.warning(f"   Emploi.ma RSS error: {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 8 — Tanqeeb RSS feeds (more reliable than scraping)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_tanqeeb_rss() -> list[dict]:
    jobs = []
    feeds = [
        ("SA", "https://www.tanqeeb.com/rss/jobs-in-saudi-arabia.rss"),
        ("AE", "https://www.tanqeeb.com/rss/jobs-in-uae.rss"),
        ("EG", "https://www.tanqeeb.com/rss/jobs-in-egypt.rss"),
        ("MA", "https://www.tanqeeb.com/rss/jobs-in-morocco.rss"),
        ("QA", "https://www.tanqeeb.com/rss/jobs-in-qatar.rss"),
        ("KW", "https://www.tanqeeb.com/rss/jobs-in-kuwait.rss"),
    ]
    for country, url in feeds:
        log.info(f"📡 Tanqeeb RSS → {country}")
        try:
            xml_text = http_get(url)
            root = ET.fromstring(xml_text)
            items = root.findall(".//item")
            log.info(f"   {len(items)} items")
            for item in items[:MAX_PER_SOURCE]:
                try:
                    title   = (item.findtext("title") or "").strip()
                    link    = (item.findtext("link") or "").strip()
                    desc    = re.sub(r'<[^>]+>', ' ', item.findtext("description") or "").strip()[:300]
                    uid     = re.sub(r'\?.*', '', link).split("/")[-1] or title[:30]
                    company = ""
                    if " at " in title:
                        parts = title.split(" at ", 1)
                        title, company = parts[0].strip(), parts[1].strip()
                    elif " - " in title:
                        parts = title.rsplit(" - ", 1)
                        title, company = parts[0].strip(), parts[1].strip()
                    if title:
                        jobs.append(build_job("Tanqeeb", uid, title, company, country, link, desc))
                except Exception as e:
                    log.debug(f"tanqeeb rss item err: {e}")
        except Exception as e:
            log.warning(f"   Tanqeeb RSS error ({country}): {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE 9 — Bayt RSS feeds
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_bayt_rss() -> list[dict]:
    jobs = []
    feeds = [
        ("AE", "https://www.bayt.com/en/uae/jobs/rss/"),
        ("SA", "https://www.bayt.com/en/saudi-arabia/jobs/rss/"),
        ("EG", "https://www.bayt.com/en/egypt/jobs/rss/"),
        ("KW", "https://www.bayt.com/en/kuwait/jobs/rss/"),
        ("QA", "https://www.bayt.com/en/qatar/jobs/rss/"),
    ]
    for country, url in feeds:
        log.info(f"📡 Bayt RSS → {country}")
        try:
            xml_text = http_get(url)
            root = ET.fromstring(xml_text)
            items = root.findall(".//item")
            log.info(f"   {len(items)} items")
            for item in items[:MAX_PER_SOURCE]:
                try:
                    title   = (item.findtext("title") or "").strip()
                    link    = (item.findtext("link") or "").strip()
                    desc    = re.sub(r'<[^>]+>', ' ', item.findtext("description") or "").strip()[:300]
                    uid     = re.sub(r'\?.*', '', link).split("/")[-2] or title[:30]
                    company = ""
                    if " at " in title:
                        parts = title.split(" at ", 1)
                        title, company = parts[0].strip(), parts[1].strip()
                    if title:
                        jobs.append(build_job("Bayt", uid, title, company, country, link, desc))
                except Exception as e:
                    log.debug(f"bayt rss item err: {e}")
        except Exception as e:
            log.warning(f"   Bayt RSS error ({country}): {e}")
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("🚀 MENA Jobs Scraper v3 — starting")
    start = datetime.now()

    # ── RSS/API sources (no browser needed) ───────────────────────────────────
    log.info("\n── Phase 1: RSS & API sources ──")
    rss_results = await asyncio.gather(
        scrape_rekrute_rss(),
        scrape_emploima_rss(),
        scrape_tanqeeb_rss(),
        scrape_bayt_rss(),
        return_exceptions=True
    )
    rss_names = ["Rekrute RSS", "Emploi.ma RSS", "Tanqeeb RSS", "Bayt RSS"]

    # ── Browser-based sources ─────────────────────────────────────────────────
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
        browser_results = await asyncio.gather(
            scrape_linkedin(ctx),
            scrape_wuzzuf(ctx),
            scrape_bayt(ctx),
            scrape_tanqeeb(ctx),
            scrape_dreamjob(ctx),
            return_exceptions=True
        )
        await browser.close()

    browser_names = ["LinkedIn", "Wuzzuf", "Bayt", "Tanqeeb", "Dreamjob"]

    # ── Flatten all results ────────────────────────────────────────────────────
    all_jobs: list[dict] = []
    for name, r in zip(rss_names + browser_names, list(rss_results) + list(browser_results)):
        if isinstance(r, Exception):
            log.error(f"❌ {name} crashed: {r}")
        else:
            log.info(f"📦 {name}: {len(r)} jobs")
            all_jobs.extend(r)

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
    log.info(f"🏁 Done in {elapsed}s — ✅ {saved} saved | ⏭ {skipped} skipped | ❌ {failed} failed")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
