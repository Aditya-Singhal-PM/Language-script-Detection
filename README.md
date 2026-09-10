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
    tesseract-ocr-spa tesseract-ocr-rus tesseract-ocr-hin tesseract-ocr-kor \
    tesseract-ocr-ell tesseract-ocr-heb tesseract-ocr-tha tesseract-ocr-ben \
    tesseract-ocr-tam tesseract-ocr-tel tesseract-ocr-kan tesseract-ocr-mal

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

The API is job-based, not a single blocking request — scanned, multi-page,
multi-language documents (especially anything that triggers the Japanese
kana-check double-OCR pass) can take minutes, which is a poor fit for a
synchronous HTTP request. Submit a document, get a `job_id` back
immediately, then poll for the result.

### `POST /api/analyze`
Multipart upload, field name `file`. Accepts `.pdf` or `.docx`, 25 MB max.
Returns immediately (HTTP 202) with a job id — does not wait for analysis
to finish.

```bash
curl -X POST http://localhost:8000/api/analyze -F "file=@contract.pdf"
# => {"job_id": "b88ed778f3d44037a162c327bb8d3bd2"}
```

### `GET /api/jobs/{job_id}`
Poll this until `status` is `done` or `error`.

```bash
curl http://localhost:8000/api/jobs/b88ed778f3d44037a162c327bb8d3bd2
```
```json
{
  "status": "processing",
  "result": null,
  "error": null
}
```
Once finished:
```json
{
  "status": "done",
  "result": {
    "file_path": "contract.pdf",
    "total_pages": 12,
    "pages_processed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "page_results": [
      {"page_num": 1, "source": "native_text", "script": null,
       "languages": [["en", 0.999]], "text_sample": "This agreement is entered into...", "note": ""}
    ],
    "language_summary": {
      "en": {"units_dominant": 10, "pct_of_processed_units": 83.3, "avg_confidence": 0.97},
      "zh-cn": {"units_dominant": 2, "pct_of_processed_units": 16.7, "avg_confidence": 0.93}
    },
    "warnings": []
  },
  "error": null
}
```
A 404 on this endpoint means the job expired (results are kept 1 hour) or
the server instance restarted — re-submit the document.

### `GET /api/health`
Returns `{"status": "ok"}` — for load balancer / deploy-platform health checks.

### Cloud Run specific setting this requires

By default, Cloud Run only allocates CPU to an instance **while it's
actively handling a request**. This app's background analysis work keeps
running *after* `/api/analyze` returns its response — which means it can
get starved of CPU between the frontend's poll requests unless you enable:

**Service settings → Edit & Deploy New Revision → Resources/Billing → CPU
allocation → "CPU is always allocated"** (instead of the default "CPU is
only allocated during request processing").

This doesn't apply on Render, Fly.io, or a plain VM/Docker host — it's a
Cloud Run-only restriction.

### Multi-instance deployments (max-instances > 1)

The job store is pluggable for exactly this reason — see `app/job_store.py`.
By default (`JOB_STORE_BACKEND` unset or `memory`), jobs live in an
in-memory dict, which only works correctly when the service runs as a
single instance. With more than one instance, Cloud Run's load balancer
doesn't pin a client to the instance that's processing their job, so a
poll request landing on a *different* instance would incorrectly 404 a
job that's actually still running fine elsewhere.

To fix this, set `JOB_STORE_BACKEND=firestore` (as an env var on the
Cloud Run service) once you raise max-instances above 1. One-time setup:

1. In the Cloud Console, go to **Firestore** → **Create Database** →
   Native mode → pick a region (ideally the same one as your Cloud Run
   service).
2. On the Cloud Run service, add environment variable
   `JOB_STORE_BACKEND=firestore` (Edit & Deploy New Revision →
   Variables & Secrets).
3. No credential setup needed — Cloud Run's attached service account
   authenticates to Firestore automatically. (Testing the Firestore
   backend locally would need `gcloud auth application-default login`
   first, or a service account key via `GOOGLE_APPLICATION_CREDENTIALS`.)

This is free at this scale (Firestore's Always Free tier: 50K reads/20K
writes per day), and every instance now sees the same job status instead
of each holding its own private copy.



## Deploying

The app needs Tesseract at the OS level, so it deploys most cleanly
anywhere that runs a Docker image: Render, Railway, Fly.io, AWS
App Runner/ECS, Google Cloud Run, or your own VM.

Cloud Run / Fly.io / Render's "Docker" service type will build directly
from the included `Dockerfile` — point them at this repo and no extra
config is needed beyond exposing port 8000.

For a plain VM: `docker run -d -p 80:8000 --restart unless-stopped doc-lang-app`.

## What's not done yet

- **Firestore backend is untested from my end** — the code follows
  Firestore's documented client API correctly, but I don't have network
  access to Google's APIs in this sandbox to actually run it. Test it
  against a real Firestore database before relying on it for anything
  important.
- **No visual QA on the frontend** — it renders and served correctly in
  testing, but I couldn't load it in an actual browser from this
  environment. Worth a quick look once deployed before sharing it
  around.
- **No auth/rate-limiting** — fine for an internal tool behind your
  company network or VPN; add an API key check in `app/main.py` before
  exposing it publicly.
- **No history/database of past analyses** — each job is discarded after
  its TTL. Fine for a quick tool; add persistence if you want a record.
- **Script coverage was incomplete and is now verified.** `verify_osd_scripts.py`
  renders a sample per script, asks OSD what label it emits, and checks
  each label has an explicit route. Run it after a Tesseract upgrade.
  Auditing this found **seven unrouted labels** beyond the Japanese bug:
  `Korean` (the code only had `Hangul` — OSD actually emits `Korean`),
  plus `Greek`, `Thai`, `Bengali`, `Tamil`, `Telugu`, `Kannada`, and
  `Malayalam`, none of which were handled at all. Each was silently
  falling through to the Latin branch. All are now routed with their
  language packs added to the Dockerfile.
- **OSD's own confusions that no amount of routing can fix** (surfaced by
  the verify script, worth knowing before trusting a result):
  - **Hebrew is reported as `Cyrillic`** by OSD. A Hebrew document will
    therefore be OCR'd with the Russian model and produce nonsense. If
    you need Hebrew, add a Unicode-range pre-check ahead of the OSD call.
  - **Kanji-only Japanese is reported as `Han`**, not `Japanese`. The
    kana check catches Japanese *with* kana, but a page of pure kanji
    (common in formal/legal Japanese headings) will be labelled Chinese.
    Your test contract worked because most pages did contain kana.
  - OSD needs a reasonable amount of text; single-line blocks fall back
    to a Unicode-composition probe that's good but not infallible.
- Arabic routing is implemented and its OSD label verified, but not
  tested end-to-end for OCR *accuracy* (no Arabic text font available
  here to generate a legible sample) — check against a real Arabic
  document before relying on it. Same for Bengali/Kannada/Malayalam:
  label routing verified, OCR accuracy unverified.
- Simplified vs. Traditional Chinese detection is a lightweight
  character-set heuristic, not exhaustive.
- Free Tesseract accuracy on poor-quality/faint scans will be
  noticeably below a cloud OCR provider (Google Vision / Azure Document
  Intelligence) — see `_safe_ocr` in `app/lang_detect_core.py` for where
  that would slot in.
