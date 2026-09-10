"""
FastAPI backend for the Document Language Identifier.

Endpoints:
  GET  /                    -> serves the web UI
  POST /api/analyze         -> upload a PDF/DOCX, get back a job_id immediately
  GET  /api/jobs/{job_id}   -> poll for status/result of a submitted job
  GET  /api/health          -> basic health check (useful for deploy platforms)

Why a job queue instead of a blocking request/response:
Scanned, multi-page, multi-language documents (especially anything routing
through the Japanese kana-check double-OCR pass) can take minutes. A single
blocking HTTP request is a bad fit for that - the browser tab has to stay
open, there's no progress feedback, and platforms like Cloud Run kill
requests after a configurable timeout regardless. Instead, /api/analyze
kicks off the work in a background thread and returns instantly with a
job_id; the frontend polls /api/jobs/{job_id} until it's done.

IMPORTANT - Cloud Run specific: by default Cloud Run only allocates CPU to
an instance while it's actively handling a request. Background work that
continues after the request returns (exactly what this endpoint does) can
get starved of CPU between poll requests unless you enable "CPU is always
allocated" in the service's settings (Cloud Run console -> Edit & Deploy
New Revision -> under Resources / Billing). Other hosts (Render, Fly.io,
a plain VM) don't have this restriction - it's a Cloud-Run-only setting.

This job store is in-memory and single-process, which is the right amount
of infrastructure for a small internal tool - but it means: (a) jobs are
lost if the instance restarts mid-processing, and (b) if you ever raise
max-instances above 1, a poll request could land on a different instance
than the one processing the job and get a false 404. Fine at max-instances=1
(the default we set up); worth swapping for a shared store (e.g. Redis) if
you scale beyond that.
"""
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.job_store import get_job_store
from app.lang_detect_core import analyze_document

ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB - generous for a quick app; raise if needed

app = FastAPI(title="Document Language Identifier")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your actual frontend origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

# See app/job_store.py for why this is pluggable: an in-memory dict only
# works correctly at max-instances: 1, since a poll request isn't
# guaranteed to land on the same instance that's processing the job.
# Set JOB_STORE_BACKEND=firestore once you raise max-instances above 1.
job_store = get_job_store()


def _run_analysis_job(job_id: str, tmp_path: str, tmp_dir: str, original_filename: str):
    """Runs in a background thread. Never raises - always writes a
    status into the job store so the poller has something to see."""
    try:
        report = analyze_document(tmp_path)
        result = asdict(report)
        result["file_path"] = original_filename
        job_store.set_done(job_id, result)
    except Exception as e:
        job_store.set_error(job_id, f"Could not analyze document: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
async def analyze(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Upload a .pdf or .docx file.")

    job_store.cleanup_old()

    tmp_dir = tempfile.mkdtemp(prefix="doclang_")
    tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(status_code=413, detail="File too large (25 MB limit).")
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    job_id = uuid.uuid4().hex
    job_store.create(job_id)

    # BackgroundTasks runs this after the response is sent. Starlette runs
    # sync callables like this one in a worker thread automatically, so it
    # doesn't block the event loop from handling other requests (e.g. the
    # poll requests checking on this very job).
    background_tasks.add_task(_run_analysis_job, job_id, tmp_path, tmp_dir, file.filename)

    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired, or the server restarted).")
    return {"status": job["status"], "result": job["result"], "error": job["error"]}


# Serve the frontend last so it doesn't shadow the /api routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
