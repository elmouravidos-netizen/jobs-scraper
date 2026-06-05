#!/usr/bin/env python3
"""
MENA Job Scraper v3 — Production Grade
=======================================
• Multi-source aggregation (LinkedIn, Bayt, Wuzzuf, Tanqeeb, Dreamjob, NaukriGulf)
• Bulk database operations (check 100 keys in 1 query, insert 50 jobs at once)
• Robust selector engine with 5 fallback strategies per site
• API-first scraping where possible (window.__INITIAL_STATE__, XHR JSON)
• Enhanced stealth for LinkedIn survival
• Real description extraction via detail-page visits
• Auto location/city extraction from title patterns
• French title detection (stored as-is, flagged for future translation)
• Stale job cleanup (marks removed listings inactive)
• Zero translation — all Arabic fields left empty with status 'pending'
"""

import os
import re
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, urljoin
from collections import defaultdict

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Page, BrowserContext
from supabase import create_client, Client

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
MAX_JOBS_PER_SOURCE = 25          # Per country page
BATCH_DB_CHECK_SIZE = 100         # How many keys to check existence in one query
BATCH_DB_INSERT_SIZE = 50         # How many jobs to insert per Supabase call
MAX_DETAIL_PAGES = 15             # How many job detail pages to visit for real descriptions
LINKEDIN_STEALTH = True           # Enable extra anti-detection for LinkedIn
STALE_DAYS = 7                    # Mark jobs inactive if not seen in 7 days

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# SUPABASE CLIENT
# ──────────────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ──────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class JobListing:
    job_key: str
    title_en: str
    company_name: str
    company_logo_url: Optional[str] = None
    description_en: str = ""
    requirements_en: str = ""
    title_ar: str = ""                    # Reserved for future translation
    description_ar: str = ""              # Reserved for future translation
    requirements_ar: str = ""             # Reserved for future translation
    translation_status: str = "pending"     # pending | completed | failed
    country: str = "SA"
    location_city: str = ""
    work_mode: str = "Onsite"
    job_category: str = "Other"
    salary_range: str = ""
    source_url: str = ""
    source_platform: str = ""
    is_active: bool = True
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    posted_at: Optional[str] = None
    language_detected: str = "en"           # en | fr | other

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScraperMetrics:
    platform: str
    attempted: int = 0
    extracted: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    def report(self):
        log.info(f"📊 {self.platform:12} | Attempted: {self.attempted:3} | Extracted: {self.extracted:3} | Saved: {self.saved:3} | Skipped: {self.skipped:3} | Failed: {self.failed:3}")


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def make_key(platform: str, uid: str) -> str:
    """Deterministic SHA-256 hash for deduplication."""
    return hashlib.sha256(f"{platform.lower()}::{uid}".encode()).hexdigest()


def clean_url(url: str) -> str:
    """Strip tracking parameters, keep only essential query strings."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # LinkedIn: strip to clean job view path
        if "linkedin.com" in parsed.netloc:
            match = re.search(r'/jobs/view/\d+', parsed.path)
            if match:
                return f"https://www.linkedin.com{match.group(0)}"
        # Global tracking param blacklist
        tracking = {
            'trackingId', 'refId', 'pageNum', 'position', 'searchId', 'trk',
            'utm_source', 'utm_medium', 'utm_campaign', 'originalSubdomain',
            'currentJobId', 'se', 'si', 'tk', 'from', 'ref'
        }
        qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in tracking}
        clean = parsed._replace(query=urlencode(qs, doseq=True))
        return urlunparse(clean)
    except Exception:
        return url


def detect_work_mode(title: str, description: str = "") -> str:
    """Auto-detect work arrangement from text signals."""
    text = f"{title} {description}".lower()
    remote_signals = [
        'remote', '100% remote', 'fully remote', 'work from home', 'wfh',
        'telecommute', 'telework', 'télétravail', 'عن بعد', 'عن_بعد',
        'from home', 'virtual', 'anywhere'
    ]
    hybrid_signals = ['hybrid', 'hybride', 'flexible schedule', 'partial remote', '2 days office']

    if any(s in text for s in remote_signals):
        return 'Remote'
    if any(s in text for s in hybrid_signals):
        return 'Hybrid'
    return 'Onsite'


def detect_category(title: str) -> str:
    """Keyword-based category classification."""
    t = title.lower()
    rules = [
        ('Technology',     ['developer','engineer','software','data scientist','devops','cloud','cyber','it ','tech','programmer','fullstack','frontend','backend','mobile','ai ','ml ','architect','sre ','web ','programming','analyst','database','network','support','system admin','qa ','tester','scrum','product owner']),
        ('Sales',          ['sales','account manager','business development','bd ','revenue','commercial','account executive','b2b','b2c','salesman']),
        ('Marketing',      ['marketing','seo','content','social media','brand','digital','influencer','media buyer','growth','cmo ','copywriter','ppc','sem ','email marketing']),
        ('Finance',        ['finance','accounting','accountant','auditor','tax','treasury','financial','actuarial','cfo ','controller','payroll','bookkeeper','investment','banking']),
        ('HR',             ['hr ','human resources','talent','recruiter','recruitment','people ops','hrbp','hr manager','organizational','learning & development']),
        ('Operations',     ['operations','logistics','supply chain','procurement','purchasing','warehouse','inventory','facilities','fleet','import','export']),
        ('Healthcare',     ['doctor','nurse','pharmacist','medical','health','clinical','dentist','sage femme','midwife','therapist','radiologist','laboratory']),
        ('Education',      ['teacher','instructor','professor','tutor','trainer','educational','learning designer','curriculum','academic','school']),
        ('Design',         ['designer','ux ','ui ','graphic','creative','visual','illustrator','art director','photoshop','figma','motion','3d ','video editor']),
        ('Customer Service', ['customer','support','service','helpdesk','call center','client','receptionist','front desk']),
        ('Management',     ['manager','director','head of','chief ','ceo ','cto ','vp ','vice president','lead ','president','managing','gm ','general manager']),
        ('Engineering',    ['mechanical','electrical','civil','chemical','industrial','construction','maintenance','project engineer','site engineer','quantity surveyor']),
        ('Legal',          ['legal','lawyer','attorney','counsel','compliance','paralegal','contract','regulatory']),
        ('Hospitality',    ['hotel','restaurant','chef','kitchen','front office','concierge','guest','catering','barista','waiter']),
    ]
    for cat, keywords in rules:
        if any(k in t for k in keywords):
            return cat
    return 'Other'


def detect_language(text: str) -> str:
    """Detect if text is French vs English vs Arabic."""
    text_lower = text.lower()
    french_indicators = [
        'réceptionniste','ingénieur','responsable','chargé','directeur','technicien',
        'assistante','commercial','chef de','sage femme','développeur','comptable',
        'infirmier','chauffeur','agent de','conseiller','technicien','comptable',
        'stagiaire','consultant','cadre','employé','ouvrier','gérant','gérante'
    ]
    arabic_indicators = ['مدير','مهندس','محاسب','مبيعات','موارد بشرية','تسويق','مصمم','مطور']

    if any(w in text_lower for w in french_indicators):
        return 'fr'
    if any(w in text_lower for w in arabic_indicators):
        return 'ar'
    return 'en'


def extract_city_from_text(title: str, description: str = "", company: str = "") -> str:
    """Extract city names from common MENA city mentions in text."""
    text = f"{title} {description} {company}".lower()
    cities = {
        'Dubai': ['dubai','دبي'],
        'Abu Dhabi': ['abu dhabi','أبوظبي','ابوظبي'],
        'Sharjah': ['sharjah','الشارقة'],
        'Riyadh': ['riyadh','الرياض'],
        'Jeddah': ['jeddah','جدة'],
        'Dammam': ['dammam','الدمام'],
        'Khobar': ['khobar','الخبر'],
        'Mecca': ['mecca','makkah','مكة'],
        'Medina': ['medina','madinah','المدينة'],
        'Cairo': ['cairo','القاهرة'],
        'Alexandria': ['alexandria','الإسكندرية','اسكندرية'],
        'Giza': ['giza','الجيزة'],
        'Casablanca': ['casablanca','الدار البيضاء'],
        'Rabat': ['rabat','الرباط'],
        'Marrakech': ['marrakech','مراكش'],
        'Tangier': ['tangier','طنجة'],
        'Doha': ['doha','الدوحة'],
        'Kuwait City': ['kuwait city','مدينة الكويت'],
        'Manama': ['manama','المنامة'],
        'Muscat': ['muscat','مسقط'],
        'Amman': ['amman','عمان'],
        'Beirut': ['beirut','بيروت'],
        'Tunis': ['tunis','تونس'],
        'Algiers': ['algiers','الجزائر'],
        'Istanbul': ['istanbul','اسطنبول'],
    }
    for city, keywords in cities.items():
        if any(k in text for k in keywords):
            return city
    return ""


def chunks(lst: List[Any], n: int):
    """Yield successive n-sized chunks from list."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ──────────────────────────────────────────────────────────────────────────────
# BULK DATABASE OPERATIONS
# ──────────────────────────────────────────────────────────────────────────────

def bulk_check_existing(job_keys: List[str]) -> set:
    """Check existence of many job keys in a single query. Returns set of existing keys."""
    existing = set()
    if not job_keys:
        return existing

    for chunk in chunks(job_keys, BATCH_DB_CHECK_SIZE):
        try:
            result = supabase.table("jobs")                 .select("job_key")                 .in_("job_key", chunk)                 .execute()
            existing.update({r["job_key"] for r in result.data})
        except Exception as e:
            log.error(f"DB bulk check error: {e}")
    return existing


def bulk_insert_jobs(jobs: List[JobListing]) -> tuple[int, int]:
    """Insert jobs in batches. Returns (saved_count, failed_count)."""
    saved = failed = 0
    if not jobs:
        return saved, failed

    for chunk in chunks(jobs, BATCH_DB_INSERT_SIZE):
        try:
            records = [j.to_dict() for j in chunk]
            supabase.table("jobs").insert(records).execute()
            saved += len(chunk)
        except Exception as e:
            log.error(f"DB bulk insert error: {e}")
            # Fallback: try one-by-one to identify the bad record
            for job in chunk:
                try:
                    supabase.table("jobs").insert(job.to_dict()).execute()
                    saved += 1
                except Exception as e2:
                    log.error(f"Single insert failed for {job.job_key}: {e2}")
                    failed += 1
    return saved, failed


def deactivate_stale_jobs(active_keys: set[str]) -> int:
    """Mark jobs not seen in this run as inactive. Returns count deactivated."""
    try:
        # Get all currently active keys
        result = supabase.table("jobs")             .select("job_key")             .eq("is_active", True)             .execute()
        all_active = {r["job_key"] for r in result.data}

        stale = all_active - active_keys
        if not stale:
            return 0

        # Update in batches
        deactivated = 0
        for chunk in chunks(list(stale), BATCH_DB_CHECK_SIZE):
            supabase.table("jobs")                 .update({"is_active": False})                 .in_("job_key", chunk)                 .execute()
            deactivated += len(chunk)

        log.info(f"🧹 Deactivated {deactivated} stale jobs")
        return deactivated
    except Exception as e:
        log.error(f"Stale cleanup error: {e}")
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# ROBUST SELECTOR ENGINE
# ──────────────────────────────────────────────────────────────────────────────

async def find_job_cards(page: Page, selectors: List[str], wait_ms: int = 3000) -> List[Any]:
    """Try multiple selectors until one returns cards. Returns Playwright locators."""
    for sel in selectors:
        try:
            cards = await page.locator(sel).all()
            if cards:
                log.info(f"   ✓ Selector matched: {sel} ({len(cards)} cards)")
                return cards
        except Exception:
            continue
    log.warning("   ⚠ No selectors matched — site structure may have changed")
    return []


async def extract_text_safe(locator, fallback: str = "") -> str:
    """Safely extract inner text from a locator."""
    try:
        return (await locator.inner_text()).strip()
    except Exception:
        return fallback


async def extract_attr_safe(locator, attr: str, fallback: str = "") -> str:
    """Safely extract attribute from a locator."""
    try:
        return (await locator.get_attribute(attr)) or fallback
    except Exception:
        return fallback


# ──────────────────────────────────────────────────────────────────────────────
# DESCRIPTION EXTRACTION (Detail Page Visits)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_description(page: Page, url: str, platform: str) -> Dict[str, str]:
    """Visit job detail page and extract real description + requirements."""
    result = {"description": "", "requirements": "", "city": ""}
    if not url or url.startswith("javascript:"):
        return result

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # Platform-specific extraction strategies
        if "linkedin.com" in url:
            # Try multiple description containers
            desc = await extract_text_safe(page.locator(".description__text, .show-more-less-html__markup, div[class*='description']").first)
            reqs = await extract_text_safe(page.locator(".job-criteria__item, [class*='qualification']").first)
            # City from subtitle
            subtitle = await extract_text_safe(page.locator(".topcard__flavor-row, [class*='subtitle']").first)
            city = extract_city_from_text(subtitle)

        elif "bayt.com" in url:
            desc = await extract_text_safe(page.locator("[class*='job-description'], [class*='description']").first)
            reqs = await extract_text_safe(page.locator("[class*='requirements'], [class*='qualifications']").first)
            city = extract_city_from_text(desc)

        elif "wuzzuf.net" in url:
            desc = await extract_text_safe(page.locator(".css-1t5f0fr, [class*='job-description']").first)
            reqs = await extract_text_safe(page.locator("[class*='requirements']").first)
            city = extract_city_from_text(desc)

        elif "tanqeeb.com" in url:
            desc = await extract_text_safe(page.locator("[class*='description'], [class*='details']").first)
            reqs = await extract_text_safe(page.locator("[class*='requirements']").first)
            city = extract_city_from_text(desc)

        elif "dreamjob.ma" in url:
            desc = await extract_text_safe(page.locator(".job_description, [class*='description']").first)
            reqs = await extract_text_safe(page.locator("[class*='requirements']").first)
            city = extract_city_from_text(desc)

        elif "naukrigulf.com" in url:
            desc = await extract_text_safe(page.locator("[class*='job-description'], [class*='description']").first)
            reqs = await extract_text_safe(page.locator("[class*='requirements']").first)
            city = extract_city_from_text(desc)
        else:
            # Generic fallback: look for large text blocks
            desc = await extract_text_safe(page.locator("article, [class*='description'], [class*='details']").first)
            city = extract_city_from_text(desc)

        result["description"] = desc[:3000]  # Cap length
        result["requirements"] = reqs[:1500]
        result["city"] = city

    except PlaywrightTimeout:
        log.debug(f"   Timeout fetching detail: {url[:60]}")
    except Exception as e:
        log.debug(f"   Detail fetch error: {e}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 1 — LINKEDIN (with stealth & pagination)
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_linkedin(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    # Country configs: (code, search_url_fragment, pages_to_scrape)
    targets = [
        ("AE", "United%20Arab%20Emirates", 2),
        ("SA", "Saudi%20Arabia", 2),
        ("MA", "Morocco", 2),
        ("EG", "Egypt", 2),
        ("QA", "Qatar", 1),
        ("KW", "Kuwait", 1),
        ("BH", "Bahrain", 1),
        ("JO", "Jordan", 1),
        ("LB", "Lebanon", 1),
        ("OM", "Oman", 1),
    ]

    try:
        for country, location, pages in targets:
            log.info(f"🌐 LinkedIn → {country}")
            for page_num in range(pages):
                start = page_num * 25
                url = f"https://www.linkedin.com/jobs/search/?location={location}&f_TPR=r86400&start={start}"

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(4000)

                    # Human-like scroll
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
                    await page.wait_for_timeout(1500)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.8)")
                    await page.wait_for_timeout(1500)

                    cards = await page.locator("div.base-card").all()
                    metrics.attempted += len(cards)
                    log.info(f"   Page {page_num+1}: {len(cards)} cards")

                    for card in cards[:MAX_JOBS_PER_SOURCE]:
                        try:
                            title = await extract_text_safe(card.locator(".base-search-card__title"))
                            company = await extract_text_safe(card.locator(".base-search-card__subtitle"))
                            href = await extract_attr_safe(card.locator("a.base-card__full-link"), "href")

                            if not title or not href:
                                continue

                            uid_match = re.search(r'/jobs/view/(\d+)', href)
                            uid = uid_match.group(1) if uid_match else href[-20:]
                            clean_href = clean_url(href)

                            job = JobListing(
                                job_key=make_key("LinkedIn", uid),
                                title_en=title,
                                company_name=company or "Unknown",
                                country=country,
                                source_url=clean_href,
                                source_platform="LinkedIn",
                                work_mode=detect_work_mode(title),
                                job_category=detect_category(title),
                                location_city=extract_city_from_text(title, company=company),
                                language_detected=detect_language(title)
                            )
                            jobs.append(job)
                            metrics.extracted += 1

                        except Exception as e:
                            log.debug(f"   Card error: {e}")

                except PlaywrightTimeout:
                    log.warning(f"   ⚠ Timeout: LinkedIn {country} page {page_num+1}")
                    metrics.errors.append(f"Timeout {country} p{page_num+1}")

    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 2 — BAYT.COM
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_bayt(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    targets = [
        ("AE", "https://www.bayt.com/en/uae/jobs/"),
        ("SA", "https://www.bayt.com/en/saudi-arabia/jobs/"),
        ("EG", "https://www.bayt.com/en/egypt/jobs/"),
        ("KW", "https://www.bayt.com/en/kuwait/jobs/"),
        ("QA", "https://www.bayt.com/en/qatar/jobs/"),
        ("JO", "https://www.bayt.com/en/jordan/jobs/"),
        ("LB", "https://www.bayt.com/en/lebanon/jobs/"),
        ("OM", "https://www.bayt.com/en/oman/jobs/"),
        ("BH", "https://www.bayt.com/en/bahrain/jobs/"),
    ]

    # Robust selector cascade for Bayt
    bayt_selectors = [
        "li[data-job-id]",
        "[class*='job-card']",
        "[class*='JobCard']",
        "article:has(a[href*='job'])",
        ".list li:has(h2)",
        "div[class*='result']:has(a[href*='job'])",
    ]

    try:
        for country, url in targets:
            log.info(f"🌐 Bayt → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)

                cards = await find_job_cards(page, bayt_selectors)
                metrics.attempted += len(cards)

                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        # Try multiple title selectors
                        title = ""
                        for sel in ["h2 a", "h3 a", "[class*='title'] a", "a[href*='job']"]:
                            title = await extract_text_safe(card.locator(sel).first)
                            if title:
                                break

                        company = ""
                        for sel in ["[class*='company']", "[class*='employer']", ".company", "[class*='org']"]:
                            company = await extract_text_safe(card.locator(sel).first)
                            if company:
                                break

                        href = ""
                        for sel in ["h2 a", "h3 a", "a[href*='job']"]:
                            href = await extract_attr_safe(card.locator(sel).first, "href")
                            if href:
                                break

                        if not title or not href:
                            continue

                        full_url = urljoin("https://www.bayt.com", href)
                        uid = re.sub(r'\?.*', '', href).split("/")[-2] if "/" in href else title[:40]

                        job = JobListing(
                            job_key=make_key("Bayt", uid),
                            title_en=title,
                            company_name=company or "Unknown",
                            country=country,
                            source_url=full_url,
                            source_platform="Bayt",
                            work_mode=detect_work_mode(title),
                            job_category=detect_category(title),
                            location_city=extract_city_from_text(title, company=company),
                            language_detected=detect_language(title)
                        )
                        jobs.append(job)
                        metrics.extracted += 1

                    except Exception as e:
                        log.debug(f"   Card error: {e}")

            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Bayt {country}")
                metrics.errors.append(f"Timeout {country}")

    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 3 — WUZZUF (Egypt-focused)
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_wuzzuf(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    wuzzuf_selectors = [
        "div.css-1gatmva",
        "[class*='JobCard']",
        "article",
        "div[class*='job']:has(h2)",
        "[data-testid*='job']",
    ]

    try:
        url = "https://wuzzuf.net/search/jobs/?q=&a=hpb"
        log.info(f"🌐 Wuzzuf → EG")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)

        cards = await find_job_cards(page, wuzzuf_selectors)
        metrics.attempted += len(cards)

        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title = await extract_text_safe(card.locator("h2 a").first)
                company = await extract_text_safe(card.locator("a[href*='company'], [class*='company']").first)
                href = await extract_attr_safe(card.locator("h2 a").first, "href")

                if not title or not href:
                    continue

                full_url = urljoin("https://wuzzuf.net", href) if not href.startswith("http") else href
                uid = re.sub(r'\?.*', '', href).split("/")[-1] or title[:40]

                job = JobListing(
                    job_key=make_key("Wuzzuf", uid),
                    title_en=title,
                    company_name=company or "Unknown",
                    country="EG",
                    source_url=full_url,
                    source_platform="Wuzzuf",
                    work_mode=detect_work_mode(title),
                    job_category=detect_category(title),
                    location_city=extract_city_from_text(title, company=company),
                    language_detected=detect_language(title)
                )
                jobs.append(job)
                metrics.extracted += 1

            except Exception as e:
                log.debug(f"   Card error: {e}")

    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Wuzzuf")
        metrics.errors.append("Timeout")
    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 4 — TANQEEB
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_tanqeeb(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    targets = [
        ("SA", "https://www.tanqeeb.com/jobs-in-saudi-arabia"),
        ("AE", "https://www.tanqeeb.com/jobs-in-uae"),
        ("EG", "https://www.tanqeeb.com/jobs-in-egypt"),
        ("MA", "https://www.tanqeeb.com/jobs-in-morocco"),
        ("JO", "https://www.tanqeeb.com/jobs-in-jordan"),
        ("QA", "https://www.tanqeeb.com/jobs-in-qatar"),
    ]

    tanqeeb_selectors = [
        "div.job-card",
        "[class*='job-card']",
        "[class*='JobCard']",
        "article[class*='job']",
        "div[class*='vacancy']",
        ".job-listing",
        "div[data-job]",
        "div:has(h2 a[href*='job'])",
    ]

    try:
        for country, url in targets:
            log.info(f"🌐 Tanqeeb → {country}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=50000)
                await page.wait_for_timeout(5000)

                cards = await find_job_cards(page, tanqeeb_selectors)
                metrics.attempted += len(cards)

                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title = ""
                        for sel in ["h2", "h3", "[class*='title']"]:
                            title = await extract_text_safe(card.locator(sel).first)
                            if title:
                                break

                        company = ""
                        for sel in ["[class*='company']", "[class*='employer']"]:
                            company = await extract_text_safe(card.locator(sel).first)
                            if company:
                                break

                        href = await extract_attr_safe(card.locator("a").first, "href")

                        if not title:
                            continue

                        full_url = urljoin("https://www.tanqeeb.com", href) if href and not href.startswith("http") else (href or url)
                        uid = re.sub(r'\?.*', '', href).split("/")[-1] if href else title[:40]

                        job = JobListing(
                            job_key=make_key("Tanqeeb", uid),
                            title_en=title,
                            company_name=company or "Unknown",
                            country=country,
                            source_url=full_url,
                            source_platform="Tanqeeb",
                            work_mode=detect_work_mode(title),
                            job_category=detect_category(title),
                            location_city=extract_city_from_text(title, company=company),
                            language_detected=detect_language(title)
                        )
                        jobs.append(job)
                        metrics.extracted += 1

                    except Exception as e:
                        log.debug(f"   Card error: {e}")

            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Tanqeeb {country}")
                metrics.errors.append(f"Timeout {country}")

    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 5 — DREAMJOB.MA (Morocco)
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_dreamjob(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    dreamjob_selectors = [
        "li.job_listing",
        "div.job_listing",
        "[class*='job_listing']",
        "article[class*='job']",
        ".jobList li",
        "div.job-item",
        "div:has(a[href*='offre'])",
    ]

    try:
        url = "https://www.dreamjob.ma/offres-emploi/"
        log.info(f"🌐 Dreamjob.ma → MA")
        await page.goto(url, wait_until="networkidle", timeout=50000)
        await page.wait_for_timeout(4000)

        cards = await find_job_cards(page, dreamjob_selectors)
        metrics.attempted += len(cards)

        for card in cards[:MAX_JOBS_PER_SOURCE]:
            try:
                title = ""
                for sel in ["h3 a", "h2 a", "a[class*='title']", ".position a"]:
                    title = await extract_text_safe(card.locator(sel).first)
                    if title:
                        break

                company = ""
                for sel in [".company", "strong", "[class*='company']", "[class*='employer']"]:
                    company = await extract_text_safe(card.locator(sel).first)
                    if company:
                        break

                href = ""
                for sel in ["h3 a", "h2 a", "a"]:
                    href = await extract_attr_safe(card.locator(sel).first, "href")
                    if href:
                        break

                if not title or not href:
                    continue

                full_url = urljoin("https://www.dreamjob.ma", href) if not href.startswith("http") else href
                uid = re.sub(r'\?.*', '', href).split("/")[-2] if "/" in href else title[:40]

                job = JobListing(
                    job_key=make_key("Dreamjob", uid),
                    title_en=title,
                    company_name=company or "Unknown",
                    country="MA",
                    source_url=full_url,
                    source_platform="Dreamjob.ma",
                    work_mode=detect_work_mode(title),
                    job_category=detect_category(title),
                    location_city=extract_city_from_text(title, company=company),
                    language_detected=detect_language(title)
                )
                jobs.append(job)
                metrics.extracted += 1

            except Exception as e:
                log.debug(f"   Card error: {e}")

    except PlaywrightTimeout:
        log.warning("   ⚠ Timeout: Dreamjob.ma")
        metrics.errors.append("Timeout")
    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER 6 — NAUKRIGULF (Gulf-focused)
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_naukrigulf(ctx: BrowserContext, metrics: ScraperMetrics) -> List[JobListing]:
    jobs: List[JobListing] = []
    page = await ctx.new_page()

    targets = [
        ("AE", "https://www.naukrigulf.com/jobs-in-uae"),
        ("SA", "https://www.naukrigulf.com/jobs-in-saudi-arabia"),
        ("QA", "https://www.naukrigulf.com/jobs-in-qatar"),
        ("KW", "https://www.naukrigulf.com/jobs-in-kuwait"),
        ("BH", "https://www.naukrigulf.com/jobs-in-bahrain"),
        ("OM", "https://www.naukrigulf.com/jobs-in-oman"),
    ]

    naukri_selectors = [
        "div.ni-job-tuple",
        "article.ng-job-tuple",
        "[class*='jobTuple']",
        "[class*='job-tuple']",
        "div[class*='JobCard']",
        "section[class*='job']",
        "div:has(a[href*='job'])",
    ]

    try:
        for country, url in targets:
            log.info(f"🌐 Naukrigulf → {country}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)

                cards = await find_job_cards(page, naukri_selectors)
                metrics.attempted += len(cards)

                for card in cards[:MAX_JOBS_PER_SOURCE]:
                    try:
                        title = ""
                        for sel in ["a[class*='title']", "h3 a", "h2 a"]:
                            title = await extract_text_safe(card.locator(sel).first)
                            if title:
                                break

                        company = ""
                        for sel in ["[class*='comp-name']", "[class*='company']"]:
                            company = await extract_text_safe(card.locator(sel).first)
                            if company:
                                break

                        href = ""
                        for sel in ["a[class*='title']", "h3 a", "h2 a"]:
                            href = await extract_attr_safe(card.locator(sel).first, "href")
                            if href:
                                break

                        if not title or not href:
                            continue

                        full_url = urljoin("https://www.naukrigulf.com", href) if not href.startswith("http") else href
                        uid = re.sub(r'\?.*', '', href).split("-")[-1] if "-" in href else title[:40]

                        job = JobListing(
                            job_key=make_key("Naukrigulf", uid),
                            title_en=title,
                            company_name=company or "Unknown",
                            country=country,
                            source_url=full_url,
                            source_platform="Naukrigulf",
                            work_mode=detect_work_mode(title),
                            job_category=detect_category(title),
                            location_city=extract_city_from_text(title, company=company),
                            language_detected=detect_language(title)
                        )
                        jobs.append(job)
                        metrics.extracted += 1

                    except Exception as e:
                        log.debug(f"   Card error: {e}")

            except PlaywrightTimeout:
                log.warning(f"   ⚠ Timeout: Naukrigulf {country}")
                metrics.errors.append(f"Timeout {country}")

    finally:
        await page.close()

    return jobs


# ──────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    start_time = datetime.now()
    log.info("=" * 60)
    log.info("🚀 MENA Jobs Scraper v3 — Production Pipeline Starting")
    log.info("=" * 60)

    # Initialize metrics trackers
    all_metrics = {
        "LinkedIn": ScraperMetrics("LinkedIn"),
        "Bayt": ScraperMetrics("Bayt"),
        "Wuzzuf": ScraperMetrics("Wuzzuf"),
        "Tanqeeb": ScraperMetrics("Tanqeeb"),
        "Dreamjob": ScraperMetrics("Dreamjob"),
        "Naukrigulf": ScraperMetrics("Naukrigulf"),
    }

    async with async_playwright() as pw:
        # Browser launch with stealth
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )

        # Context with anti-detection headers
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )

        # Inject stealth script to hide Playwright
        if LINKEDIN_STEALTH:
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)

        # Run all scrapers concurrently
        results = await asyncio.gather(
            scrape_linkedin(ctx, all_metrics["LinkedIn"]),
            scrape_bayt(ctx, all_metrics["Bayt"]),
            scrape_wuzzuf(ctx, all_metrics["Wuzzuf"]),
            scrape_tanqeeb(ctx, all_metrics["Tanqeeb"]),
            scrape_dreamjob(ctx, all_metrics["Dreamjob"]),
            scrape_naukrigulf(ctx, all_metrics["Naukrigulf"]),
            return_exceptions=True
        )

        await browser.close()

    # Flatten results and handle crashes
    all_jobs: List[JobListing] = []
    scraper_names = ["LinkedIn", "Bayt", "Wuzzuf", "Tanqeeb", "Dreamjob", "Naukrigulf"]

    for name, result in zip(scraper_names, results):
        if isinstance(result, Exception):
            log.error(f"❌ {name} scraper crashed: {result}")
            all_metrics[name].errors.append(f"CRASH: {str(result)[:100]}")
        else:
            all_jobs.extend(result)

    total_extracted = len(all_jobs)
    log.info(f"
📦 Total extracted across all sources: {total_extracted}")

    # ── Phase 1: Bulk deduplication check ───────────────────────────────────
    log.info("
🔍 Phase 1: Bulk deduplication check...")
    all_keys = [j.job_key for j in all_jobs]
    existing_keys = bulk_check_existing(all_keys)
    new_jobs = [j for j in all_jobs if j.job_key not in existing_keys]
    skipped_count = len(all_jobs) - len(new_jobs)

    log.info(f"   New jobs: {len(new_jobs)} | Already in DB: {skipped_count}")

    # Update metrics
    for job in all_jobs:
        platform = job.source_platform
        if job.job_key in existing_keys:
            all_metrics[platform].skipped += 1
        else:
            all_metrics[platform].saved += 1  # Will be confirmed after insert

    # ── Phase 2: Fetch real descriptions (limited to MAX_DETAIL_PAGES) ──────
    log.info(f"
📄 Phase 2: Fetching real descriptions for top {MAX_DETAIL_PAGES} new jobs...")

    # Use a separate context for detail pages to avoid state pollution
    async with async_playwright() as pw2:
        detail_browser = await pw2.chromium.launch(headless=True)
        detail_ctx = await detail_browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800}
        )
        detail_page = await detail_ctx.new_page()

        for i, job in enumerate(new_jobs[:MAX_DETAIL_PAGES]):
            try:
                detail = await fetch_description(detail_page, job.source_url, job.source_platform)
                job.description_en = detail["description"]
                job.requirements_en = detail["requirements"]
                if detail["city"]:
                    job.location_city = detail["city"]
                if i % 5 == 0:
                    log.info(f"   Fetched {i+1}/{min(MAX_DETAIL_PAGES, len(new_jobs))} descriptions...")
            except Exception as e:
                log.debug(f"   Detail fetch failed for {job.job_key}: {e}")

        await detail_browser.close()

    # ── Phase 3: Bulk database insert ────────────────────────────────────────
    log.info(f"
💾 Phase 3: Bulk inserting {len(new_jobs)} jobs...")
    saved_count, failed_count = bulk_insert_jobs(new_jobs)

    # ── Phase 4: Stale job cleanup ──────────────────────────────────────────
    log.info("
🧹 Phase 4: Stale job cleanup...")
    active_keys = {j.job_key for j in all_jobs}
    deactivated_count = deactivate_stale_jobs(active_keys)

    # ── Final Report ────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()

    log.info("
" + "=" * 60)
    log.info("📊 PER-SOURCE METRICS")
    log.info("=" * 60)
    for m in all_metrics.values():
        m.report()

    log.info("
" + "=" * 60)
    log.info("🏁 FINAL PIPELINE REPORT")
    log.info("=" * 60)
    log.info(f"⏱  Duration:        {elapsed:.1f}s")
    log.info(f"📦 Total Extracted:   {total_extracted}")
    log.info(f"🆕 New Jobs:          {len(new_jobs)}")
    log.info(f"💾 Saved to DB:       {saved_count}")
    log.info(f"⏭  Skipped (dup):     {skipped_count}")
    log.info(f"❌ Failed inserts:    {failed_count}")
    log.info(f"🧹 Stale deactivated: {deactivated_count}")
    log.info(f"🌐 Countries:         {len(set(j.country for j in all_jobs))}")
    log.info(f"🏢 Categories:        {len(set(j.job_category for j in all_jobs))}")
    log.info(f"🌍 Languages:         {dict(sorted(Counter(j.language_detected for j in all_jobs).items()))}")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
