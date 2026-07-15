"""
ONE-TIME BACKFILL: Translate Jooble/Adzuna descriptions to Arabic using Gemini (FREE)
Finds jobs with description_en but no description_ar, translates them.
Usage:
export GEMINI_API_KEY="your_key"
python translate_jooble_adzuna.py
"""
import os
import re
import time
import logging
import google.generativeai as genai
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not set. Get one at https://aistudio.google.com/apikey")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ─────────────────────────────────────────────────────────────────
BATCH_SIZE = 10
PAUSE_BETWEEN = 2.0
FETCH_LIMIT = 500  # jobs per run

def translate_description(title_en: str, description_en: str) -> str:
    """Translate job description to Arabic using Gemini."""
    clean_desc = re.sub(r'<[^>]+>', ' ', description_en or '').strip()
    clean_desc = re.sub(r'\s+', ' ', clean_desc)[:1500]
    
    prompt = f"""You are a professional HR translator. Translate this job posting into clear, professional Arabic.

Job Title: {title_en}
Job Description: {clean_desc}

Return ONLY the Arabic translation, no explanations or extra text."""
    
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
    result = (
        supabase.table("jobs")
        .select("id, title_en, description_en, source_platform")
        .in_("source_platform", ["Jooble", "Adzuna"])
        .eq("is_active", True)
        .not_("description_en", "is", None)
        .is_("description_ar", None)
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
        
        if desc_ar:
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
            failed += 1
        
        # Pause to respect rate limits
        if i < len(jobs) - 1:
            time.sleep(PAUSE_BETWEEN)
    
    log.info(f"\n{'='*55}")
    log.info(f"🏁 Complete!")
    log.info(f"   ✅ Translated: {translated}")
    log.info(f"   ❌ Failed: {failed}")
    log.info(f"{'='*55}")

if __name__ == "__main__":
    main()
