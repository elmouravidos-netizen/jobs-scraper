# 🚀 MENA Jobs Scraper — Autonomous Arabic Job Aggregation Pipeline

An enterprise-grade, serverless pipeline that automatically scrapes job vacancies from 5 major MENA job boards, translates them into professional Arabic via Google Gemini, and stores them in Supabase — fully automated on GitHub Actions at zero cost.

---

## 🎯 Covered Platforms

| Platform | Countries | Notes |
|---|---|---|
| **Tanqeeb** | SA, AE, QA, EG | Largest Gulf-focused board |
| **Bayt.com** | AE, SA, EG, KW | Premium MENA professionals |
| **Wuzzuf** | EG | Egypt's #1 job board |
| **LinkedIn** | AE, SA, MA, EG | Public listings (no login) |
| **Dreamjob.ma** | MA | Morocco's top board |

---

## 🏗️ Architecture

```
[ 5 Job Boards ]
      │
      ▼  GitHub Actions (every 6 hrs, free)
┌─────────────────────────────────┐
│  Playwright Headless Browser    │
│  Python 3.11 Scraper Engine     │
└─────────────────────────────────┘
      │
      ▼  API call per new job
┌─────────────────────────────────┐
│  Google Gemini 1.5 Flash        │
│  Professional Arabic Translation│
└─────────────────────────────────┘
      │
      ▼  SHA-256 dedup → upsert
┌─────────────────────────────────┐
│  Supabase PostgreSQL            │
│  Row-Level Security enabled     │
└─────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────┐
│  Next.js 14 Arabic Frontend     │
│  Hosted on Hostinger            │
└─────────────────────────────────┘
```

---

## ⚙️ GitHub Secrets Required

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Where to find it |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API |
| `GEMINI_API_KEY` | Google AI Studio → Get API Key |

---

## 🛠️ Local Testing

```bash
git clone https://github.com/YOUR_USERNAME/mena-jobs-scraper
cd mena-jobs-scraper

pip install -r requirements.txt
npx playwright install chromium --with-deps

export SUPABASE_URL="your_url"
export SUPABASE_SERVICE_ROLE_KEY="your_key"
export GEMINI_API_KEY="your_key"

python scraper.py
```

---

## 📊 Key Features

- **Zero duplicates** — SHA-256 dedup key per job, checked before any translation call
- **Retry logic** — Gemini translation retries 3× with exponential back-off
- **Rate limiting** — 0.5s buffer between saves to respect API limits
- **Per-platform error isolation** — one failing scraper never stops the others
- **Free compute** — GitHub Actions free tier (2,000 min/month) is enough for 4× daily runs
