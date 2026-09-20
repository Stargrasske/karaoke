import os
import sqlite3
import time
from typing import Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, BackgroundTasks, HTTPException

from . import staging
from . import localcache
from . import jobs
from .lyrics import Lyrics
from .whisper_sync import transcribe_to_lrc
from .stage_sources import stage_youtube_captions

router = APIRouter()

class StageYoutubeRequest(BaseModel):
    url: str

class StageWhisperRequest(BaseModel):
    file_path: str
    artist: str
    title: str

def run_stage_youtube_task(url: str):
    try:
        stage_youtube_captions(url)
    except Exception as e:
        # Background logging or safe fail
        pass

def run_stage_whisper_task(file_path: str, artist: str, title: str):
    try:
        # Transcribe audio file to LRC text using Whisper
        lrc = transcribe_to_lrc(file_path)
        lyrics = Lyrics(plain="", synced_raw=lrc, source="whisper")
        # Stage the result
        with localcache.connect() as conn:
            staging.stage_lyrics(
                artist=artist,
                title=title,
                lyrics=lyrics,
                source_kind="whisper",
                source_url=file_path,
                conn=conn
            )
    except Exception as e:
        pass

@router.post("/api/staging/youtube")
def stage_youtube(req: StageYoutubeRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    job_id = jobs.create_job()
    background_tasks.add_task(jobs.run_job, job_id, run_stage_youtube_task, req.url)
    return {
        "status": "accepted",
        "message": "YouTube caption staging job dispatched to background supervisor.",
        "job_id": job_id,
    }

@router.post("/api/staging/whisper")
def stage_whisper(req: StageWhisperRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")
    job_id = jobs.create_job()
    background_tasks.add_task(
        jobs.run_job,
        job_id,
        run_stage_whisper_task,
        req.file_path,
        req.artist,
        req.title
    )
    return {
        "status": "accepted",
        "message": "Whisper transcription job dispatched to background supervisor.",
        "job_id": job_id,
    }


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    """Poll status for a background job returned by an `accepted`-status endpoint."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
