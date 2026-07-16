"""
SHARED SCRAPER UTILITIES
Functions used by both scraper.py (MENA jobs) and
scraper_intl.py (international visa-sponsored jobs).
Keeping these in one place means improvements to translation,
deduplication, or category detection benefit both scrapers
automatically with zero duplicate maintenance.

TRANSLATION STRATEGY:
- PRIMARY: Google Gemini (FREE, fast, excellent Arabic)
- FALLBACK: OpenRouter Qwen 2.5 72B (if Gemini fails)
"""
import os
import re
import json
import hashlib
import asyncio
import logging
import urllib.request
import google.generativeai as genai
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

log = logging.getLogger(__name__)

# ── Gemini Configuration ─────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    log.info("✅ Gemini AI ENABLED (primary)")
else:
    gemini_model = None
    log.warning("⚠️  GEMINI_API_KEY not set — will use OpenRouter only")

OPENROUTER_MODEL = "qwen/qwen-2.5-72b-instruct"

# ══════════════════════════════════════════════════════════════════════════════
# DEDUP / URL UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def make_key(platform: str, uid: str) -> str:
    """SHA-256 fingerprint to guarantee zero duplicate rows."""
    return hashlib.sha256(f"{platform}::{uid}".encode()).hexdigest()

def clean_url(url: str) -> str:
    """Strip tracking params, normalize LinkedIn URLs."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if "linkedin.com" in parsed.netloc:
            m = re.search(r'/jobs/view/[^/?]+', parsed.path)
            if m:
                return f"https://www.linkedin.com{m.group(0)}"
        junk = {'trackingId', 'refId', 'pageNum', 'position', 'searchId', 'trk', 'src', 'sid'}
        qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in junk}
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url

# ══════════════════════════════════════════════════════════════════════════════
# DETECTION UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def detect_work_mode(title: str, desc: str = "") -> str:
    t = (title + " " + desc).lower()
    if any(w in t for w in ['remote', '100% remote', 'fully remote', 'work from home', 'wfh', 'télétravail']):
        return 'Remote'
    if any(w in t for w in ['hybrid', 'hybride', 'flexible location']):
        return 'Hybrid'
    return 'Onsite'

def detect_category(title: str) -> str:
    t = title.lower()
    cats = {
        'Technology':       ['developer', 'engineer', 'software', 'data', 'devops', 'cloud', 'cyber',
                             'programmer', 'fullstack', 'frontend', 'backend', 'mobile', 'architect',
                             'sysadmin', 'network', 'database', 'qa ', 'tester', 'it ', 'tech'],
        'Sales':            ['sales', 'account manager', 'business development', 'bd ', 'commercial'],
        'Marketing':        ['marketing', 'seo', 'content', 'social media', 'brand', 'digital', 'media buyer'],
        'Finance':          ['finance', 'accounting', 'accountant', 'auditor', 'tax', 'treasury',
                             'financial', 'cfo', 'comptable', 'budget', 'controller'],
        'HR':               ['hr ', 'human resources', 'talent', 'recruiter', 'recruitment', 'payroll'],
        'Operations':       ['operations', 'logistics', 'supply chain', 'procurement',
                             'warehouse', 'inventory', 'facilities', 'fleet'],
        'Healthcare':       ['doctor', 'nurse', 'pharmacist', 'medical', 'health', 'clinical',
                             'dentist', 'sage femme', 'midwife', 'radiology', 'caregiver'],
        'Education':        ['teacher', 'instructor', 'professor', 'tutor', 'trainer', 'educational'],
        'Design':           ['designer', 'ux', 'ui ', 'graphic', 'creative', 'visual', 'figma'],
        'Customer Service': ['customer service', 'support', 'helpdesk', 'call center', 'client relations'],
        'Management':       ['manager', 'director', 'head of', 'chief', 'ceo', 'cto',
                             'vp ', 'vice president', 'general manager', 'supervisor'],
        'Engineering':      ['mechanical', 'electrical', 'civil', 'chemical', 'industrial',
                             'construction', 'maintenance', 'structural'],
        'Legal':            ['lawyer', 'legal', 'counsel', 'compliance', 'contract', 'paralegal'],
        'Admin':            ['assistant', 'secretary', 'receptionist', 'administrative', 'coordinator'],
        'Agriculture':      ['farm', 'agriculture', 'crop', 'livestock', 'harvest'],
        'Hospitality':      ['hotel', 'hospitality', 'chef', 'cook', 'waiter', 'housekeeping', 'barista'],
        'Trades':           ['electrician', 'plumber', 'welder', 'carpenter', 'mechanic', 'technician'],
    }
    for cat, kws in cats.items():
        if any(k in t for k in kws):
            return cat
    return 'Other'

# ══════════════════════════════════════════════════════════════════════════════
# HTTP UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post_json(url: str, payload: dict, headers: dict = None, timeout: int = 25) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ══════════════════════════════════════════════════════════════════════════════
# TRANSLATION (Gemini PRIMARY, OpenRouter FALLBACK)
# ══════════════════════════════════════════════════════════════════════════════

# ── Gemini Translation Functions ─────────────────────────────────────────────
async def batch_translate_titles_gemini(titles: list[str]) -> list[str]:
    """Translate job titles using Gemini (FREE)."""
    if not gemini_model or not titles:
        return []
    
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "You are a professional HR translator. "
        "Translate each numbered job title into professional Arabic. "
        "Return ONLY a numbered list in the exact same order. "
        "No explanations. No extra text.\n\n"
        f"{numbered}"
    )
    
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt,
            genai.types.GenerationConfig(temperature=0.1, max_output_tokens=600)
        )
        raw = response.text.strip()
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
        filled = sum(1 for r in results if r)
        if filled >= len(titles) * 0.5:
            log.info(f"  ✅ Gemini batch translated {filled}/{len(titles)} titles")
            return results
    except Exception as e:
        log.warning(f"  ⚠ Gemini batch failed: {e}")
    
    return []

async def translate_title_and_description_gemini(title: str, description: str) -> tuple[str, str]:
    """Translate title and description using Gemini (FREE)."""
    if not gemini_model or not title:
        return "", ""
    
    clean_desc = re.sub(r'<[^>]+>', ' ', description or '').strip()
    clean_desc = re.sub(r'\s+', ' ', clean_desc)[:1500]
    
    prompt = (
        "You are a professional HR translator specializing in international "
        "employment contracts for Arabic-speaking job seekers. "
        "Translate the following job posting into clear, professional, "
        "modern business Arabic. Preserve all technical terms, company names, "
        "and visa/program names accurately.\n\n"
        "Return your response in EXACTLY this format with no extra text:\n\n"
        "TITLE: [arabic title here]\n"
        "DESCRIPTION: [arabic description here]\n\n"
        f"Original Title: {title}\n"
        f"Original Description: {clean_desc}"
    )
    
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt,
            genai.types.GenerationConfig(temperature=0.2, max_output_tokens=1200)
        )
        raw = response.text.strip()
        title_match = re.search(r'TITLE:\s*(.+?)(?:\n|$)', raw)
        desc_match = re.search(r'DESCRIPTION:\s*(.+)', raw, re.DOTALL)
        title_ar = title_match.group(1).strip() if title_match else ""
        desc_ar = desc_match.group(1).strip() if desc_match else ""
        if title_ar:
            log.info(f"  ✅ Gemini translation successful")
            return title_ar, desc_ar
    except Exception as e:
        log.warning(f"  ⚠ Gemini translation failed: {e}")
    
    return "", ""

# ── OpenRouter Fallback Functions ────────────────────────────────────────────
async def _call_openrouter_batch(prompt: str, expected_count: int, api_key: str) -> list[str]:
    """OpenRouter fallback for batch translation."""
    if not api_key:
        return []
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/mena-jobs-scraper",
        "X-Title": "MENA Jobs Scraper",
    }
    for attempt in range(1, 4):
        try:
            resp = http_post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
            raw = resp["choices"][0]["message"]["content"].strip()
            results = [""] * expected_count
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(\d+)[.\)]\s*(.+)$', line)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < expected_count:
                        results[idx] = m.group(2).strip()
            filled = sum(1 for r in results if r)
            if filled >= expected_count * 0.5:
                return results
            await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"  OpenRouter batch attempt {attempt}/3: {e}")
            await asyncio.sleep(2 ** attempt)
    return []

async def translate_title_and_description_openrouter(title: str, description: str, api_key: str) -> tuple[str, str]:
    """OpenRouter fallback for title+description translation."""
    if not api_key or not title:
        return "", ""
    
    clean_desc = re.sub(r'<[^>]+>', ' ', description or '').strip()
    clean_desc = re.sub(r'\s+', ' ', clean_desc)[:1500]
    
    prompt = (
        "You are a professional HR translator specializing in international "
        "employment contracts for Arabic-speaking job seekers. "
        "Translate the following job posting into clear, professional, "
        "modern business Arabic. Preserve all technical terms, company names, "
        "and visa/program names accurately.\n\n"
        "Return your response in EXACTLY this format with no extra text:\n\n"
        "TITLE: [arabic title here]\n"
        "DESCRIPTION: [arabic description here]\n\n"
        f"Original Title: {title}\n"
        f"Original Description: {clean_desc}"
    )
    
    for attempt in range(1, 4):
        try:
            payload = {
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1200,
                "temperature": 0.2,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/mena-jobs-scraper",
                "X-Title": "International Jobs Translator",
            }
            resp = http_post_json("https://openrouter.ai/api/v1/chat/completions", payload, headers)
            raw = resp["choices"][0]["message"]["content"].strip()
            title_match = re.search(r'TITLE:\s*(.+?)(?:\n|$)', raw)
            desc_match = re.search(r'DESCRIPTION:\s*(.+)', raw, re.DOTALL)
            title_ar = title_match.group(1).strip() if title_match else ""
            desc_ar = desc_match.group(1).strip() if desc_match else ""
            if title_ar:
                return title_ar, desc_ar
            await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"  OpenRouter attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 ** attempt)
    return "", ""

# ── Main Dispatcher Functions (Gemini first, OpenRouter fallback) ────────────
async def batch_translate_titles(titles: list[str], api_key: str) -> list[str]:
    """
    Translate a batch of job TITLES only — used for MENA jobs.
    Tries Gemini first (FREE), falls back to OpenRouter if it fails.
    """
    if not titles:
        return [""] * len(titles)
    
    # Try Gemini first
    result = await batch_translate_titles_gemini(titles)
    if result:
        return result
    
    # Fallback to OpenRouter
    log.info("  🔄 Falling back to OpenRouter for title batch...")
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "You are a professional HR translator. "
        "Translate each numbered job title into professional Arabic. "
        "Return ONLY a numbered list in the exact same order. "
        "No explanations. No extra text.\n\n"
        f"{numbered}"
    )
    return await _call_openrouter_batch(prompt, len(titles), api_key)

async def translate_title_and_description(title: str, description: str, api_key: str) -> tuple[str, str]:
    """
    Translate BOTH title and description — used for international jobs.
    Tries Gemini first (FREE), falls back to OpenRouter if it fails.
    """
    if not title:
        return "", ""
    
    # Try Gemini first
    title_ar, desc_ar = await translate_title_and_description_gemini(title, description)
    if title_ar:
        return title_ar, desc_ar
    
    # Fallback to OpenRouter
    log.info("  🔄 Falling back to OpenRouter for title+description...")
    return await translate_title_and_description_openrouter(title, description, api_key)

# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_existing_keys(supabase, keys: list[str]) -> set[str]:
    """Batch-check which job_keys already exist — one query, not N queries."""
    if not keys:
        return set()
    try:
        result = supabase.table("jobs").select("job_key").in_("job_key", keys).execute()
        return {row["job_key"] for row in result.data}
    except Exception as e:
        log.error(f"DB key lookup error: {e}")
        return set()

def filter_new_jobs(jobs: list[dict], supabase) -> list[dict]:
    """Remove jobs already in DB using a single batch query."""
    if not jobs:
        return []
    keys = [j["job_key"] for j in jobs]
    existing = get_existing_keys(supabase, keys)
    new = [j for j in jobs if j["job_key"] not in existing]
    skipped = len(jobs) - len(new)
    if skipped:
        log.info(f"  ⏭  {skipped} already in DB, {len(new)} new")
    return new
