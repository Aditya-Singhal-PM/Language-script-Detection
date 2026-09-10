"""
FastAPI backend for the Document Language Identifier.

Endpoints:
  GET  /                -> serves the web UI
  POST /api/analyze     -> upload a PDF/DOCX, get back detected languages
  GET  /api/health       -> basic health check (useful for deploy platforms)
"""
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Upload a .pdf or .docx file.")

    # Stream to a temp file rather than loading fully into memory, and
    # enforce a size cap while doing so.
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

        try:
            report = analyze_document(tmp_path)
        except Exception as e:
            # Surface a clean error rather than a raw stack trace to the client.
            raise HTTPException(status_code=422, detail=f"Could not analyze document: {e}")

        result = asdict(report)
        result["file_path"] = file.filename  # don't leak the server temp path back to the client
        return JSONResponse(result)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# Serve the frontend last so it doesn't shadow the /api routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
