# backend/routes.py

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import FileResponse
from uuid import uuid4
from redis import Redis
from backend.tasks import clone_site

router = APIRouter()
redis  = Redis(host="localhost", port=6379, db=0, decode_responses=True)

@router.post("/clone", status_code=202)
def clone(url: str = Body(..., embed=True)):
    """
    Expects JSON body: { "url": "<website-to-clone>" }.
    Starts the Celery task and returns job_id.
    """
    jid = uuid4().hex
    redis.hset(f"jobs:{jid}", mapping={"status": "queued", "progress": 0})
    clone_site.delay(jid, url)
    return {"job_id": jid}

@router.get("/jobs/{jid}")
def job_status(jid: str):
    """
    Returns the Redis hash for job_id, e.g.:
      { "status": "running", "progress": "30", ... }
    """
    data = redis.hgetall(f"jobs:{jid}")
    if not data:
        raise HTTPException(404, "job not found")
    return data

@router.get("/clone/{job_id}/raw")
def get_clone_html(job_id: str):
    """
    When a clone job finishes, it writes index.html to disk.
    This endpoint returns that HTML file for job_id.
    """
    data = redis.hgetall(f"jobs:{job_id}")
    if not data or data.get("status") != "complete":
        raise HTTPException(404, "Clone not found or not complete")
    html_path = data.get("html_path")
    return FileResponse(html_path, media_type="text/html")
