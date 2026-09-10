# Document Language Identifier — Web App

A small web app: drop in a PDF (text-based or scanned) or a Word document,
get back the probable language(s) it contains, with a page-by-page
breakdown. FastAPI backend + a static HTML/JS frontend, callable as an
API on its own.

## Run locally

```bash
pip install -r requirements.txt
# Tesseract + language packs (same as the CLI prototype):
apt-get install tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    tesseract-ocr-jpn tesseract-ocr-ara tesseract-ocr-fra tesseract-ocr-deu \
    tesseract-ocr-spa tesseract-ocr-rus tesseract-ocr-hin tesseract-ocr-kor

uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000 for the web UI.

## Run with Docker (recommended — bundles Tesseract for you)

```bash
docker build -t doc-lang-app .
docker run -p 8000:8000 doc-lang-app
```
Open http://localhost:8000.

## API

### `POST /api/analyze`
Multipart upload, field name `file`. Accepts `.pdf` or `.docx`, 25 MB max.

```bash
curl -X POST http://localhost:8000/api/analyze \
  -F "file=@contract.pdf"
```

Response:
```json
{
  "file_path": "contract.pdf",
  "total_pages": 12,
  "pages_processed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  "page_results": [
    {
      "page_num": 1,
      "source": "native_text",
      "script": null,
      "languages": [["en", 0.999]],
      "text_sample": "This agreement is entered into...",
      "note": ""
    }
  ],
  "language_summary": {
    "en": {"units_dominant": 10, "pct_of_processed_units": 83.3, "avg_confidence": 0.97},
    "zh-cn": {"units_dominant": 2, "pct_of_processed_units": 16.7, "avg_confidence": 0.93}
  },
  "warnings": []
}
```

### `GET /api/health`
Returns `{"status": "ok"}` — for load balancer / deploy-platform health checks.

## Deploying

The app needs Tesseract at the OS level, so it deploys most cleanly
anywhere that runs a Docker image: Render, Railway, Fly.io, AWS
App Runner/ECS, Google Cloud Run, or your own VM.

Cloud Run / Fly.io / Render's "Docker" service type will build directly
from the included `Dockerfile` — point them at this repo and no extra
config is needed beyond exposing port 8000.

For a plain VM: `docker run -d -p 80:8000 --restart unless-stopped doc-lang-app`.

## What's not done yet

- **No visual QA on the frontend** — it renders and served correctly in
  testing, but I couldn't load it in an actual browser from this
  environment. Worth a quick look once deployed before sharing it
  around.
- **No auth/rate-limiting** — fine for an internal tool behind your
  company network or VPN; add an API key check in `app/main.py` before
  exposing it publicly.
- **In-memory, stateless** — each request is processed and discarded; no
  history/database. Fine for a quick tool, add persistence if you want a
  record of past analyses.
- Same known limitations as the CLI version (Arabic untested end-to-end,
  Simplified/Traditional Chinese heuristic is lightweight, free Tesseract
  vs. cloud OCR accuracy tradeoff) — see the OCR routing logic in
  `app/lang_detect_core.py` for where a cloud OCR provider would slot in.
