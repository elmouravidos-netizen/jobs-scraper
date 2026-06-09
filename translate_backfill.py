"""
ONE-TIME BACKFILL SCRIPT
------------------------
Translates all jobs in Supabase where translation_status = 'pending'
Run once locally or as a manual GitHub Actions job.

Usage:
  export SUPABASE_URL="your_url"
  export SUPABASE_SERVICE_ROLE_KEY="your_key"
  export OPENROUTER_API_KEY="your_openrouter_key"
  python translate_backfill.py
"""

import os
import re
import json
import time
import logging
import urllib.request
from supabase import create_client, Client

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

supabase: Client   = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ─────────────────────────────────────────────────────────────────────
TRANSLATE_MODEL    = "qwen/qwen2.5-72b-instruct"
BATCH_SIZE         = 15      # titles per API call
PAUSE_BETWEEN      = 1.5     # seconds between batches (rate limit safety)
FETCH_PAGE_SIZE    = 200     # jobs fetched from DB per round


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSLATION
# ══════════════════════════════════════════════════════════════════════════════

def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 25) -> dict:
    data = json.dumps(payload).encode("utf-8")
    h    = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers)
    req  = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def batch_translate(titles: list[str]) -> list[str]:
    """
    Translate a batch of job titles to Arabic in ONE API call.
    Returns Arabic strings in same order. Falls back to '' on error.
    """
    if not titles:
        return []

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt   = (
        "You are a professional HR translator. "
        "Translate each numbered job title into professional Arabic. "
        "Return ONLY a numbered list in the exact same order. "
        "No explanations. No extra text.\n\n"
        f"{numbered}"
    )
    payload  = {
        "model":       TRANSLATE_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  600,
        "temperature": 0.1,
    }
    headers  = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer":  "https://github.com/mena-jobs-scraper",
        "X-Title":       "MENA Jobs Backfill",
    }

    for attempt in range(1, 4):
        try:
            resp    = http_post_json(
                "https://openrouter.ai/api/v1/chat/completions",
                payload, headers
            )
            raw     = resp["choices"][0]["message"]["content"].strip()
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
                return results

            log.warning(f"  Low quality ({filled}/{len(titles)}), retry {attempt}")
            time.sleep(2)

        except Exception as e:
            log.warning(f"  Attempt {attempt}/3 failed: {e}")
            time.sleep(2 ** attempt)

    return [""] * len(titles)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN BACKFILL
# ══════════════════════════════════════════════════════════════════════════════

def run_backfill():
    log.info("🚀 Starting translation backfill...")

    # Count pending jobs first
    count_result = (
        supabase.table("jobs")
        .select("id", count="exact")
        .eq("translation_status", "pending")
        .execute()
    )
    total_pending = count_result.count or 0
    log.info(f"📦 Total pending jobs: {total_pending}")

    if total_pending == 0:
        log.info("✅ Nothing to translate — all jobs already translated!")
        return

    translated  = 0
    failed      = 0
    page        = 0

    while True:
        # Fetch a page of pending jobs
        offset = page * FETCH_PAGE_SIZE
        result = (
            supabase.table("jobs")
            .select("id, title_en, company_name, country")
            .eq("translation_status", "pending")
            .range(offset, offset + FETCH_PAGE_SIZE - 1)
            .execute()
        )

        jobs = result.data
        if not jobs:
            break

        log.info(f"\n📄 Page {page + 1} — {len(jobs)} jobs fetched")

        # Process in batches of BATCH_SIZE
        for i in range(0, len(jobs), BATCH_SIZE):
            batch  = jobs[i:i + BATCH_SIZE]
            titles = [j["title_en"] for j in batch]

            log.info(f"  🤖 Batch {i // BATCH_SIZE + 1} — translating {len(titles)} titles...")
            arabic = batch_translate(titles)

            # Update each job in Supabase
            for job, title_ar in zip(batch, arabic):
                status = "completed" if title_ar else "failed"
                try:
                    supabase.table("jobs").update({
                        "title_ar":           title_ar,
                        "translation_status": status,
                    }).eq("id", job["id"]).execute()

                    if title_ar:
                        log.info(f"    ✅ [{job['country']}] {job['title_en'][:40]} → {title_ar[:35]}")
                        translated += 1
                    else:
                        log.warning(f"    ⚠  [{job['country']}] {job['title_en'][:40]} — empty result")
                        failed += 1

                except Exception as e:
                    log.error(f"    ❌ DB update error: {e} — {job['title_en'][:40]}")
                    failed += 1

            # Pause between batches to respect rate limits
            if i + BATCH_SIZE < len(jobs):
                time.sleep(PAUSE_BETWEEN)

        page += 1

        # Progress report
        done = translated + failed
        pct  = (done / total_pending * 100) if total_pending else 0
        log.info(f"\n📊 Progress: {done}/{total_pending} ({pct:.1f}%) — ✅ {translated} | ❌ {failed}")

        # If we got fewer than a full page, we're done
        if len(jobs) < FETCH_PAGE_SIZE:
            break

        time.sleep(PAUSE_BETWEEN)

    log.info(f"\n{'='*55}")
    log.info(f"🏁 Backfill complete!")
    log.info(f"   ✅ Translated: {translated}")
    log.info(f"   ❌ Failed:     {failed}")
    log.info(f"   📦 Total:      {translated + failed}/{total_pending}")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    run_backfill()
