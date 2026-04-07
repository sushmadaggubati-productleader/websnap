"""
WebSnap server — FastAPI backend for the screenshot tool.

Endpoints
---------
POST /capture                Start a single capture job
GET  /status/{job_id}        Poll single job status
GET  /download/{job_id}      Download finished screenshot

POST /bulk-capture            Start a batch of capture jobs
GET  /status-bulk/{batch_id} Poll batch status
GET  /download-bulk/{batch_id} Download all completed shots as ZIP

GET  /auth/google            Redirect to Google OAuth
GET  /auth/google/callback   Google OAuth callback (sets session cookie)
GET  /auth/me                Returns current user + usage info
POST /auth/logout            Clears session cookie
POST /checkout/create        Stripe checkout (placeholder until keys are set)

GET  /                       Serve index.html

Run
---
  uvicorn server:app --reload --port 8000
"""

import io
import os
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # load .env before anything else reads os.getenv

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

import sys
sys.path.insert(0, str(Path(__file__).parent))
from agent import capture_screenshot, post_process_image, SCREENSHOTS_DIR
import auth
import design_agent
from auth import require_auth, check_usage_limit
from database import UsageRecord, get_db, init_db

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="WebSnap")

# SessionMiddleware is required by authlib for storing OAuth state between
# the /auth/google redirect and the /auth/google/callback.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-secret-please-change-in-production"),
)

# Mount auth routes (Google OAuth, /auth/me, logout, Stripe checkout)
app.include_router(auth.router)

# Create DB tables on startup
@app.on_event("startup")
async def startup():
    init_db()

# ---------------------------------------------------------------------------
# In-memory job stores (replace with Redis / DB for production)
# ---------------------------------------------------------------------------

jobs: dict[str, dict] = {}
batches: dict[str, dict] = {}
design_jobs: dict[str, dict] = {}

DESIGNS_DIR = Path("designs")
DESIGNS_DIR.mkdir(exist_ok=True)

MIME = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "gif":  "image/gif",
    "svg":  "image/svg+xml",
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CaptureRequest(BaseModel):
    url: HttpUrl
    format: str = "png"
    resize_width: Optional[int] = None
    resize_height: Optional[int] = None


class BulkCaptureRequest(BaseModel):
    urls: list[HttpUrl]
    format: str = "png"
    resize_width: Optional[int] = None
    resize_height: Optional[int] = None


# ---------------------------------------------------------------------------
# Single capture
# ---------------------------------------------------------------------------

@app.post("/capture")
async def start_capture(
    req: CaptureRequest,
    background_tasks: BackgroundTasks,
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    # Enforce free-tier limit (raises HTTP 402 if exceeded)
    check_usage_limit(1, user, db)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}

    # Record usage immediately so concurrent requests can't bypass the limit
    db.add(UsageRecord(user_id=user.id, job_id=job_id))
    db.commit()

    background_tasks.add_task(
        _run_capture, job_id, str(req.url),
        req.format, req.resize_width, req.resize_height,
    )
    return {"job_id": job_id}


@app.get("/status/{job_id}", dependencies=[Depends(require_auth)])
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return job


@app.get("/download/{job_id}", dependencies=[Depends(require_auth)])
async def download(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    if job.get("status") != "complete":
        return JSONResponse({"error": "Screenshot not ready yet"}, status_code=202)

    path = Path(job["path"])
    if not path.exists():
        return JSONResponse({"error": "File missing on disk"}, status_code=500)

    fmt = job.get("format", "png")
    media_type = MIME.get(fmt, "image/png")
    filename = job.get("filename", f"screenshot.{fmt}")
    return FileResponse(
        str(path), media_type=media_type, filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Bulk capture
# ---------------------------------------------------------------------------

@app.post("/bulk-capture")
async def start_bulk_capture(
    req: BulkCaptureRequest,
    background_tasks: BackgroundTasks,
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    num_urls = len(req.urls)

    # Enforce free-tier limit for the whole batch at once
    check_usage_limit(num_urls, user, db)

    batch_id = str(uuid.uuid4())
    job_ids = []
    for url in req.urls:
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "pending", "batch_id": batch_id, "url": str(url)}
        db.add(UsageRecord(user_id=user.id, job_id=job_id))
        background_tasks.add_task(
            _run_capture, job_id, str(url),
            req.format, req.resize_width, req.resize_height,
        )
        job_ids.append(job_id)

    db.commit()
    batches[batch_id] = {"job_ids": job_ids, "total": num_urls, "format": req.format}
    return {"batch_id": batch_id, "job_ids": job_ids}


@app.get("/status-bulk/{batch_id}", dependencies=[Depends(require_auth)])
async def get_bulk_status(batch_id: str):
    batch = batches.get(batch_id)
    if not batch:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job_statuses = [jobs.get(jid, {}) for jid in batch["job_ids"]]
    completed = sum(1 for j in job_statuses if j.get("status") == "complete")
    errored   = sum(1 for j in job_statuses if j.get("status") == "error")
    total     = batch["total"]

    return {
        "status": "complete" if (completed + errored) == total else "pending",
        "completed": completed,
        "errored": errored,
        "total": total,
        "job_ids": batch["job_ids"],
    }


@app.get("/download-bulk/{batch_id}", dependencies=[Depends(require_auth)])
async def download_bulk(batch_id: str):
    batch = batches.get(batch_id)
    if not batch:
        return JSONResponse({"error": "Batch not found"}, status_code=404)

    fmt = batch.get("format", "png")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, job_id in enumerate(batch["job_ids"], start=1):
            job = jobs.get(job_id)
            if job and job.get("status") == "complete":
                path = Path(job["path"])
                if path.exists():
                    zf.write(str(path), f"screenshot_{i:02d}.{fmt}")

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="screenshots.zip"'},
    )


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _run_capture(
    job_id: str,
    url: str,
    fmt: str = "png",
    resize_width: Optional[int] = None,
    resize_height: Optional[int] = None,
) -> None:
    fmt = fmt.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"

    ext = fmt
    raw_filename = f"{job_id}_raw.png"
    out_filename = f"{job_id}.{ext}"
    out_path     = str(SCREENSHOTS_DIR / out_filename)

    result = await capture_screenshot(url, raw_filename)
    if not result["success"]:
        jobs[job_id] = {"status": "error", "error": result.get("error", "Unknown error"), "url": url}
        return

    try:
        converted = post_process_image(
            result["path"], out_path,
            fmt=fmt,
            resize_width=resize_width,
            resize_height=resize_height,
        )
        if fmt != "png":
            Path(result["path"]).unlink(missing_ok=True)

        jobs[job_id] = {
            "status": "complete",
            "path": converted["path"],
            "filename": out_filename,
            "format": fmt,
            "width_px": converted["final_width"],
            "height_px": converted["final_height"],
            "file_size_bytes": converted["file_size_bytes"],
            "url": url,
        }
    except Exception as exc:
        jobs[job_id] = {"status": "error", "error": str(exc), "url": url}


# ---------------------------------------------------------------------------
# Design endpoints  (screenshot → functional website)
# ---------------------------------------------------------------------------

@app.post("/design/{job_id}", dependencies=[Depends(require_auth)])
async def start_design(job_id: str, background_tasks: BackgroundTasks):
    """Start the design agent for a completed screenshot job."""
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Screenshot job not found."}, status_code=404)
    if job.get("status") != "complete":
        return JSONResponse({"error": "Screenshot not ready yet."}, status_code=400)
    if job.get("format", "png") == "svg":
        return JSONResponse({"error": "SVG format is not supported for website generation."}, status_code=400)

    design_job_id = str(uuid.uuid4())
    design_jobs[design_job_id] = {
        "status": "running",
        "stage": "analyzing",
        "detail": "Starting the design agent…",
        "iterations": 0,
        "source_job_id": job_id,
    }

    background_tasks.add_task(_run_design, design_job_id, job["path"])
    return {"design_job_id": design_job_id}


@app.get("/design-status/{design_job_id}", dependencies=[Depends(require_auth)])
async def get_design_status(design_job_id: str):
    dj = design_jobs.get(design_job_id)
    if not dj:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return dj


@app.get("/design-download/{design_job_id}", dependencies=[Depends(require_auth)])
async def download_design(design_job_id: str):
    dj = design_jobs.get(design_job_id)
    if not dj:
        return JSONResponse({"error": "Design job not found."}, status_code=404)
    if dj.get("status") != "complete":
        return JSONResponse({"error": "Design not ready yet."}, status_code=202)

    html_path = Path(dj["html_path"])
    if not html_path.exists():
        return JSONResponse({"error": "HTML file missing on disk."}, status_code=500)

    return FileResponse(
        str(html_path),
        media_type="text/html",
        filename="index.html",
        headers={"Content-Disposition": 'attachment; filename="index.html"'},
    )


async def _run_design(design_job_id: str, screenshot_path: str) -> None:
    async def _progress(stage: str, detail: str) -> None:
        dj = design_jobs.get(design_job_id)
        if dj:
            dj["stage"]  = stage
            dj["detail"] = detail

    output_dir = str(DESIGNS_DIR / design_job_id)
    try:
        result = await design_agent.design_website(screenshot_path, output_dir, _progress)
        if result["success"]:
            design_jobs[design_job_id].update({
                "status":     "complete",
                "html_path":  result["html_path"],
                "iterations": result["iterations"],
                "summary":    result.get("summary", ""),
            })
        else:
            design_jobs[design_job_id].update({
                "status": "error",
                "error":  result.get("error", "Unknown error"),
            })
    except Exception as exc:
        design_jobs[design_job_id].update({"status": "error", "error": str(exc)})


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(str(Path(__file__).parent / "index.html"))
