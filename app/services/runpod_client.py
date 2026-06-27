"""
Client for the RunPod Serverless BeatSync engine worker (see runpod_worker/).

In production the C++ engine runs on RunPod (Linux), not on Railway. When
RUNPOD_ENDPOINT_ID + RUNPOD_API_KEY are set, `beat_sync_pipeline` sends the
montage job here; the worker returns the finished montage URL. If unset or the
call fails, the pipeline falls back to the local engine, then the Python path.
"""
import os
import time
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID")
# Accept RUNPOD_KEY (what's set on Railway) or RUNPOD_API_KEY.
RUNPOD_API_KEY = os.environ.get("RUNPOD_KEY") or os.environ.get("RUNPOD_API_KEY")


def is_available() -> bool:
    return bool(RUNPOD_ENDPOINT_ID and RUNPOD_API_KEY)


async def run_montage(payload: dict, timeout: float = 600.0) -> str | None:
    """Run a beat-sync montage on the RunPod worker. Returns the URL or None."""
    if not is_available():
        return None
    base = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/runsync", json={"input": payload}, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "COMPLETED":
                return _url(data)
            # runsync can exceed its sync window and return a job id to poll.
            job_id = data.get("id")
            if data.get("status") in ("IN_QUEUE", "IN_PROGRESS") and job_id:
                return await _poll(client, base, headers, job_id, timeout)
            logger.error(f"RunPod montage returned no url: {data}")
            return None
    except Exception as e:
        logger.error(f"RunPod montage call failed: {e}")
        return None


def _url(data: dict) -> str | None:
    out = data.get("output") or {}
    if out.get("error"):
        logger.error(f"RunPod worker error: {out['error']}")
        return None
    return out.get("url")


async def _poll(client, base, headers, job_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        r = await client.get(f"{base}/status/{job_id}", headers=headers)
        d = r.json()
        st = d.get("status")
        if st == "COMPLETED":
            return _url(d)
        if st in ("FAILED", "CANCELLED", "TIMED_OUT"):
            logger.error(f"RunPod job {job_id} {st}: {d.get('error')}")
            return None
    logger.error(f"RunPod job {job_id} timed out after {timeout}s")
    return None
