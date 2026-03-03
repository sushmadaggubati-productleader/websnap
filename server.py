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

GET  /                       Serve index.html

Run
---
  uvicorn server:app --reload --port 8000
"""

import io
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl

import sys
sys.path.insert(0, str(Path(__file__).parent))
from agent import capture_screenshot, post_process_image, SCREENSHOTS_DIR

app = FastAPI(title="WebSnap")

# In-memory stores (replace with Redis / DB for production)
jobs: dict[str, dict] = {}
batches: dict[str, dict] = {}

MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
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
async def start_capture(req: CaptureRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending"}
    background_tasks.add_task(
        _run_capture, job_id, str(req.url),
        req.format, req.resize_width, req.resize_height,
    )
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return job


@app.get("/download/{job_id}")
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
    return FileResponse(str(path), media_type=media_type, filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Bulk capture
# ---------------------------------------------------------------------------

@app.post("/bulk-capture")
async def start_bulk_capture(req: BulkCaptureRequest, background_tasks: BackgroundTasks):
    batch_id = str(uuid.uuid4())
    job_ids = []
    for url in req.urls:
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "pending", "batch_id": batch_id, "url": str(url)}
        background_tasks.add_task(
            _run_capture, job_id, str(url),
            req.format, req.resize_width, req.resize_height,
        )
        job_ids.append(job_id)
    batches[batch_id] = {"job_ids": job_ids, "total": len(job_ids), "format": req.format}
    return {"batch_id": batch_id, "job_ids": job_ids}


@app.get("/status-bulk/{batch_id}")
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


@app.get("/download-bulk/{batch_id}")
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

    ext = fmt  # svg / gif / jpg / png
    raw_filename  = f"{job_id}_raw.png"
    out_filename  = f"{job_id}.{ext}"
    out_path      = str(SCREENSHOTS_DIR / out_filename)

    # 1. Capture raw PNG
    result = await capture_screenshot(url, raw_filename)
    if not result["success"]:
        jobs[job_id] = {"status": "error", "error": result.get("error", "Unknown error"),
                        "url": url}
        return

    # 2. Convert format + resize
    try:
        converted = post_process_image(
            result["path"], out_path,
            fmt=fmt,
            resize_width=resize_width,
            resize_height=resize_height,
        )
        # Clean up raw PNG when we produced a different format
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
# Static
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(str(Path(__file__).parent / "index.html"))
