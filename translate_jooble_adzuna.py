"""
ONE-TIME BACKFILL: Translate Jooble/Adzuna descriptions to Arabic using Gemini (FREE)
Finds jobs with description_en but no description_ar, translates them.

Usage:
export SUPABASE_URL="your_url"
export SUPABASE_SERVICE_ROLE_KEY="your_key"
export GEMINI_API_KEY="your_gemini_key"
python translate_jooble_adzuna.py
"""
import os
import re
import time
import logging
import warnings
import google.generativeai as genai
from supabase import create_client, Client

# Suppress the deprecation warning (the package still works perfectly fine)
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_API_KEY]):
    raise ValueError("❌ Missing environment variables. Ensure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and GEMINI_API_KEY are set.")

genai.configure(api_key=GEMINI_API_KEY)
# Using gemini-1.5-flash as it is the most stable, fastest, and 100% free model
model = genai.GenerativeModel("gemini-1.5-flash")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ─────────────────────────────────────────────────────────────────
FETCH_LIMIT = 500      # jobs per run to stay well within GitHub Actions limits
PAUSE_BETWEEN = 1.5    # seconds between requests to be safe with rate limits

def translate_description(title_en: str, description_en: str) -> str:
    """Translate job description to Arabic using Gemini."""
    # Clean HTML tags and normalize whitespace
    clean_desc = re.sub(r'<[^>]+>', ' ', description_en or '').strip()
    clean_desc = re.sub(r'\s+', ' ', clean_desc)[:1500] # Cap length for cost/speed
    
    prompt = f"""You are a professional HR translator. Translate this job posting into clear, professional, modern business Arabic.

Job Title: {title_en}
Job Description: {clean_desc}

Instructions:
1. Return ONLY the Arabic translation.
2. Do not include any explanations, introductory text, or extra notes.
3. Preserve technical terms and company names accurately."""
    
    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=1000
            )
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"  ❌ Translation error: {e}")
        return ""

def main():
    log.info("🚀 Starting Jooble/Adzuna description translation...")
    
    # Fetch jobs needing translation
    # FIXED: Use .not_.is_("column", "null") for IS NOT NULL in modern supabase-py
    result = (
        supabase.table("jobs")
        .select("id, title_en, description_en, source_platform")
        .in_("source_platform", ["Jooble", "Adzuna"])
        .eq("is_active", True)
        .not_.is_("description_en", "null")       # <-- CORRECTED SYNTAX
        .is_("description_ar", "null")            # <-- CORRECTED SYNTAX
        .limit(FETCH_LIMIT)
        .execute()
    )
    
    jobs = result.data or []
    log.info(f"📦 Found {len(jobs)} jobs needing Arabic translation")
    
    if not jobs:
        log.info("✅ All Jooble/Adzuna jobs already have Arabic descriptions!")
        return
    
    translated = failed = 0
    
    for i, job in enumerate(jobs):
        log.info(f"\n[{i+1}/{len(jobs)}] {job['title_en'][:50]}")
        
        desc_ar = translate_description(job['title_en'], job['description_en'])
        
        if desc_ar and len(desc_ar) > 20: # Sanity check to ensure we got real text
            try:
                supabase.table("jobs").update({
                    "description_ar": desc_ar,
                    "translation_status": "completed"
                }).eq("id", job["id"]).execute()
                log.info(f"  ✅ Translated ({len(desc_ar)} chars)")
                translated += 1
            except Exception as e:
                log.error(f"  ❌ DB update error: {e}")
                failed += 1
        else:
            log.warning(f"  ⚠️ Empty or too short translation result, skipping update")
            failed += 1
        
        # Pause to respect rate limits
        if i < len(jobs) - 1:
            time.sleep(PAUSE_BETWEEN)
    
    log.info(f"\n{'='*55}")
    log.info(f"🏁 Complete!")
    log.info(f"   ✅ Translated: {translated}")
    log.info(f"   ❌ Failed/Skipped: {failed}")
    log.info(f"{'='*55}")

if __name__ == "__main__":
    main()
