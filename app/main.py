from fastapi import FastAPI, BackgroundTasks, HTTPException, File, UploadFile, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import uuid
import time
from datetime import datetime, timezone
import asyncio
import shutil
import os
import json
import httpx
from app.supabase_client import supabase
from app.services.beat_service import analyze_audio_beats
from app.services.fal_service import run_scail2_animation, extract_and_stylize_first_frame, build_reference_prompt, interpolate_with_rife, upscale_video
from app.services.ffmpeg_service import compose_final_video
from app.services import beatsync_engine
from app import credits

app = FastAPI(title="BeatSync AI Backend", description="API for processing video with SCAIL-2")

@app.get("/")
async def root():
    return {"status": "ok", "service": "BeatSync AI Backend"}

# Base URL the mobile client uses to reach this server. Override via env so the
# app keeps working when the dev machine's LAN IP changes or in production.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://192.168.1.169:8000")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Royalty-owned psytrance library (the user's own mastered tracks).
os.makedirs("songs", exist_ok=True)
app.mount("/songs", StaticFiles(directory="songs"), name="songs")
SONGS = []
SONGS_BY_ID = {}
_songs_manifest = "songs/manifest.json"
if os.path.exists(_songs_manifest):
    with open(_songs_manifest) as f:
        try:
            SONGS = json.load(f)
            SONGS_BY_ID = {s["id"]: s for s in SONGS}
        except Exception as e:
            print(f"Could not load songs manifest: {e}")

# Beat-sync montage target length (seconds) — a few of the track's beats, not all.
MONTAGE_SECONDS = float(os.environ.get("MONTAGE_SECONDS", "30"))

# User-uploaded tracks (ephemeral, not part of the public library list).
uploaded_songs = {}

def _get_song(song_id: str):
    """Resolve a song id from the library or the user's uploads."""
    return SONGS_BY_ID.get(song_id) or uploaded_songs.get(song_id)

class GenerateCharacterRequest(BaseModel):
    user_id: str
    video_url: str
    style_id: str
    mode: str = "single_character"
    props: Optional[str] = None
    preview_image_url: Optional[str] = None

class PreviewRequest(BaseModel):
    video_url: str
    style_id: str
    props: Optional[str] = None

class BeatSyncRequest(BaseModel):
    user_id: str
    # Ordered list of the user's rendered clip URLs to cut between on the beat.
    clip_urls: List[str]
    song_id: str
    mode: str = "beat_sync"
    # Where in the track to start the montage (seconds). None = auto intro-skip.
    start_sec: Optional[float] = None

# A beat-sync montage needs enough clips to be worthwhile, and a sane upper cap.
BEAT_SYNC_MIN_CLIPS = 5
BEAT_SYNC_MAX_CLIPS = 20

class RenderResponse(BaseModel):
    job_id: str
    status: str
    message: str

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    result_url: Optional[str] = None

@app.post("/api/v1/upload")
async def upload_video(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    # Strip any path components from the client-supplied filename to prevent
    # path traversal (e.g. a filename of "../../etc/passwd" escaping uploads/).
    safe_name = os.path.basename(file.filename or "video.mp4")
    file_path = f"uploads/{file_id}_{safe_name}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"video_url": f"{PUBLIC_BASE_URL}/{file_path}"}

@app.post("/api/v1/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    # Store an uploaded track so it gets the same waveform + section-pick flow.
    sid = f"upload_{uuid.uuid4().hex[:8]}"
    safe = os.path.basename(file.filename or "track.m4a")
    ext = os.path.splitext(safe)[1] or ".m4a"
    dest = f"songs/{sid}{ext}"
    with open(dest, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    bpm, dur = 0, 0
    if beatsync_engine.is_available():
        grid = await asyncio.to_thread(beatsync_engine.analyze_audio, dest, 0)
        if grid:
            bpm = round(grid.get("bpm", 0))
            dur = round(grid.get("duration", 0))

    song = {
        "id": sid, "title": os.path.splitext(safe)[0], "artist": "Your Upload",
        "bpm": bpm, "duration": dur, "file": os.path.basename(dest),
    }
    uploaded_songs[sid] = song
    return {**song, "url": f"{PUBLIC_BASE_URL}/songs/{song['file']}"}

@app.get("/api/v1/songs")
async def get_songs():
    # Each entry: id, title, artist, bpm, duration, plus a playable URL.
    return [
        {**s, "url": f"{PUBLIC_BASE_URL}/songs/{s['file']}"}
        for s in SONGS
    ]

@app.get("/api/v1/songs/{song_id}/waveform")
async def get_song_waveform(song_id: str):
    # Powers the section scrubber so the user can pick where the montage starts.
    song = _get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    audio_path = os.path.join("songs", song["file"])
    wf = await asyncio.to_thread(beatsync_engine.get_waveform, audio_path, 500)
    if not wf:
        raise HTTPException(status_code=500, detail="Could not read waveform")
    return {"peaks": wf["peaks"], "duration": wf["duration"], "bpm": song.get("bpm")}

@app.post("/api/v1/preview-frame")
async def generate_preview_frame(request: PreviewRequest):
    video_url_to_process = request.video_url
    if video_url_to_process == "mock_video_upload_url":
        video_url_to_process = f"{PUBLIC_BASE_URL}/uploads/example_video_small.mp4"

    # Use the clean-character reference prompt so the preview the user approves
    # is exactly the isolated character SCAIL-2 needs as its reference image.
    prompt = build_reference_prompt(request.style_id, request.props)

    preview_url = await extract_and_stylize_first_frame(video_url_to_process, prompt)
    if not preview_url:
        raise HTTPException(status_code=500, detail="Failed to generate preview frame")
        
    return {"preview_url": preview_url}

DB_FILE = "mock_db.json"

if os.path.exists(DB_FILE):
    with open(DB_FILE, "r") as f:
        try:
            mock_db = json.load(f)
        except:
            mock_db = {}
else:
    mock_db = {}

def save_mock_db():
    with open(DB_FILE, "w") as f:
        json.dump(mock_db, f)

# Beat Sync unlocks once a user has generated this many clips.
BEAT_SYNC_UNLOCK_THRESHOLD = 5

def db_create_job(job_id: str, job_data: dict) -> None:
    """Create a new job in Supabase if enabled, with local mock fallback."""
    if credits.enabled():
        try:
            db_data = {
                "id": job_id,
                "user_id": job_data["user_id"],
                "status": job_data["status"],
                "type": job_data["type"],
                "progress": job_data["progress"],
                "created_at": job_data["created_at"],
            }
            if "style_id" in job_data:
                db_data["style_id"] = job_data["style_id"]
            if "song_id" in job_data:
                db_data["song_id"] = job_data["song_id"]
            if "input_video_url" in job_data:
                db_data["input_video_url"] = job_data["input_video_url"]
            if "props" in job_data:
                db_data["props"] = job_data["props"]
            if "clip_urls" in job_data:
                db_data["clip_urls"] = job_data["clip_urls"]
            if "start_sec" in job_data:
                db_data["start_sec"] = job_data["start_sec"]
            
            supabase.table("render_jobs").insert(db_data).execute()
        except Exception as e:
            print(f"Supabase create_job failed for {job_id}: {e}")
    
    mock_db[job_id] = job_data
    save_mock_db()

def db_update_job(job_id: str, status: str, progress: int, output_url: str = None, error: str = None) -> None:
    """Update a job's status and progress in Supabase if enabled, with local mock fallback."""
    if credits.enabled():
        try:
            update_data = {
                "status": status,
                "progress": progress,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            if output_url:
                update_data["output_video_url"] = output_url
            if error:
                update_data["error"] = error
            
            supabase.table("render_jobs").update(update_data).eq("id", job_id).execute()
        except Exception as e:
            print(f"Supabase update_job failed for {job_id}: {e}")
            
    if job_id in mock_db:
        mock_db[job_id]["status"] = status
        mock_db[job_id]["progress"] = progress
        if output_url:
            mock_db[job_id]["output_video_url"] = output_url
        if error:
            mock_db[job_id]["error"] = error
        save_mock_db()

def db_get_job(job_id: str) -> Optional[dict]:
    """Retrieve a job by ID from Supabase if enabled, with local mock fallback."""
    if credits.enabled():
        try:
            res = supabase.table("render_jobs").select("*").eq("id", job_id).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            print(f"Supabase get_job failed for {job_id}: {e}")
    return mock_db.get(job_id)

def db_get_all_jobs(user_id: Optional[str] = None) -> List[dict]:
    """Retrieve all jobs (optionally filtered by user_id) from Supabase if enabled, with local mock fallback."""
    if credits.enabled():
        try:
            query = supabase.table("render_jobs").select("*")
            if user_id:
                query = query.eq("user_id", user_id)
            res = query.order("created_at", desc=True).execute()
            if res.data:
                return res.data
        except Exception as e:
            print(f"Supabase get_all_jobs failed: {e}")
            
    jobs_list = list(mock_db.values())
    if user_id:
        jobs_list = [j for j in jobs_list if j.get("user_id") == user_id]
    jobs_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jobs_list

def db_get_user_stats(user_id: str) -> dict:
    """Retrieve user statistics (completed character generation clips count) from Supabase if enabled, with local mock fallback."""
    if credits.enabled():
        try:
            res = supabase.table("render_jobs")\
                .select("id", count="exact")\
                .eq("user_id", user_id)\
                .eq("type", "generate_character")\
                .eq("status", "completed")\
                .execute()
            clip_count = res.count or 0
            return {
                "clip_count": clip_count,
                "beat_sync_threshold": BEAT_SYNC_UNLOCK_THRESHOLD,
                "beat_sync_unlocked": clip_count >= BEAT_SYNC_UNLOCK_THRESHOLD,
            }
        except Exception as e:
            print(f"Supabase get_user_stats failed for {user_id}: {e}")
            
    clip_count = sum(
        1 for j in mock_db.values()
        if j.get("user_id") == user_id
        and j.get("type") == "generate_character"
        and j.get("status") == "completed"
    )
    return {
        "clip_count": clip_count,
        "beat_sync_threshold": BEAT_SYNC_UNLOCK_THRESHOLD,
        "beat_sync_unlocked": clip_count >= BEAT_SYNC_UNLOCK_THRESHOLD,
    }

@app.post("/api/v1/generate_character", response_model=RenderResponse)
async def submit_generate_character_job(request: GenerateCharacterRequest, background_tasks: BackgroundTasks):
    # Generating a clip costs 1 credit (beat-sync montages are free). This also
    # enforces the free-tier trial window. No-op until Supabase is configured.
    if not credits.consume_credit(request.user_id):
        raise HTTPException(
            status_code=402,
            detail="Out of credits. Subscribe or top up to keep generating.",
        )

    job_id = str(uuid.uuid4())

    job_data = {
        "id": job_id,
        "user_id": request.user_id,
        "status": "pending",
        "input_video_url": request.video_url,
        "style_id": request.style_id,
        "type": "generate_character",
        "progress": 0,
        "output_video_url": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db_create_job(job_id, job_data)
    
    background_tasks.add_task(generate_character_pipeline, job_id, request)
    return {"job_id": job_id, "status": "queued", "message": "Character generation job submitted successfully."}

@app.post("/api/v1/beat_sync", response_model=RenderResponse)
async def submit_beat_sync_job(request: BeatSyncRequest, background_tasks: BackgroundTasks):
    n = len(request.clip_urls)
    if n < BEAT_SYNC_MIN_CLIPS:
        raise HTTPException(
            status_code=400,
            detail=f"Beat Sync needs at least {BEAT_SYNC_MIN_CLIPS} clips (got {n}).",
        )
    if n > BEAT_SYNC_MAX_CLIPS:
        raise HTTPException(
            status_code=400,
            detail=f"Beat Sync allows at most {BEAT_SYNC_MAX_CLIPS} clips (got {n}).",
        )

    job_id = str(uuid.uuid4())

    job_data = {
        "id": job_id,
        "user_id": request.user_id,
        "status": "pending",
        "clip_urls": request.clip_urls,
        "input_video_url": request.clip_urls[0],  # first clip = thumbnail/back-compat
        "song_id": request.song_id,
        "type": "beat_sync",
        "progress": 0,
        "output_video_url": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    db_create_job(job_id, job_data)

    background_tasks.add_task(beat_sync_pipeline, job_id, request)
    return {"job_id": job_id, "status": "queued", "message": "Beat sync job submitted successfully."}

@app.get("/api/v1/job/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    job = db_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    return {
        "job_id": job_id,
        "status": job.get("status", "pending"),
        "progress": job.get("progress", 0),
        "result_url": job.get("output_video_url")
    }

async def generate_character_pipeline(job_id: str, request: GenerateCharacterRequest):
    try:
        def update_job(status: str, progress: int, output_url: str = None):
            db_update_job(job_id, status, progress, output_url)

        video_url_to_process = request.video_url
        if video_url_to_process == "mock_video_upload_url":
            # Fallback to a mock upload if the frontend didn't pass one
            video_url_to_process = f"{PUBLIC_BASE_URL}/uploads/example_video_small.mp4"

        # SCAIL-2 caps the driving video at 160 frames, so re-encode any local
        # input to a short, fixed-fps clip well under that. RE-ENCODE (not copy)
        # and a proper filename (handles .mov etc.) so the cap is always applied.
        local_path = None
        for folder in ["uploads", "songs"]:
            if f"/{folder}/" in video_url_to_process:
                idx = video_url_to_process.index(f"/{folder}/")
                local_path = video_url_to_process[idx:].lstrip("/")
                break

        if local_path:
            base, _ = os.path.splitext(local_path)
            trimmed_path = f"{base}_scail.mp4"
            secs = os.getenv("SCAIL2_INPUT_SECONDS", "5")  # 5s @ 24fps = 120 frames < 160
            if not os.path.exists(trimmed_path):
                print(f"Re-encoding {local_path} -> {secs}s @24fps for SCAIL-2 (<=160 frames)...")
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-i", local_path, "-t", secs, "-r", "24", "-an",
                    "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                    "-y", trimmed_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process.communicate()
            video_url_to_process = f"{PUBLIC_BASE_URL}/{trimmed_path}"
        
        async def on_fal_progress(status: str, percent: int):
            update_job(status, percent)

        update_job("preparing", 0)
        animated_video_url = await run_scail2_animation(
            video_url=video_url_to_process,
            style_id=request.style_id,
            mode=request.mode,
            props=request.props,
            preview_image_url=request.preview_image_url,
            on_progress=on_fal_progress
        )

        # Auto-smooth every clip: interpolate SCAIL-2's 16fps output with RIFE.
        # Falls back to the original clip if interpolation fails (never blocks).
        final_video_url = await interpolate_with_rife(animated_video_url, on_progress=on_fal_progress)

        update_job("completed", 100, final_video_url)
        print(f"Generate character job {job_id} completed successfully!")

    except Exception as e:
        print(f"Job {job_id} failed: {str(e)}")
        # The render failed — give the spent credit back.
        credits.refund_credit(request.user_id)
        db_update_job(job_id, status="failed", progress=0, error=str(e)[:500])

async def _download_clips_local(clip_urls, job_id: str):
    """Download remote clip URLs to local files (the engine needs local paths)."""
    paths = []
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for i, url in enumerate(clip_urls):
            if not url.startswith("http"):
                if os.path.exists(url):
                    paths.append(url)
                continue
            dest = f"uploads/{job_id}_clip_{i}.mp4"
            try:
                r = await client.get(url)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    f.write(r.content)
                paths.append(dest)
            except Exception as e:
                print(f"clip download failed {url}: {e}")
    return paths

async def beat_sync_pipeline(job_id: str, request: BeatSyncRequest):
    try:
        def update_job(status: str, progress: int, output_url: str = None):
            db_update_job(job_id, status, progress, output_url)

        async def on_fal_progress(status: str, percent: int):
            update_job(status, percent)

        update_job("processing_audio", 10)
        song = _get_song(request.song_id)

        # Preferred path: the real BeatSync C++ engine — real beat detection off
        # the actual track, multi-clip cut, audio mux, and beat-synced effects.
        engine_url = None
        if beatsync_engine.is_available() and song:
            try:
                audio_path = os.path.join("songs", song["file"])
                grid = await asyncio.to_thread(
                    beatsync_engine.analyze_audio, audio_path, float(song.get("bpm", 0))
                )
                if grid and grid.get("beats"):
                    all_beats = grid["beats"]
                    # Start point: the user's chosen section if given, else skip
                    # the long dancefloor intro (per-song override / default).
                    if request.start_sec is not None:
                        skip = request.start_sec
                    else:
                        skip = float(song.get("start_sec") or os.environ.get("INTRO_SKIP_SECONDS", "60"))
                    start_beat = next((b for b in all_beats if b >= skip), all_beats[0])
                    window = [b for b in all_beats if start_beat <= b <= start_beat + MONTAGE_SECONDS]
                    beats = [b - start_beat for b in window]  # normalize montage to t=0
                    update_job("compositing", 45)
                    local_clips = await _download_clips_local(request.clip_urls, job_id)
                    if local_clips and len(beats) >= 2:
                        out_path = f"uploads/{job_id}_montage.mp4"
                        # Retry once — the engine's ffmpeg concat can fail under
                        # transient load; a retry avoids dropping to the silent path.
                        for attempt in range(2):
                            result = await asyncio.to_thread(
                                beatsync_engine.build_montage,
                                local_clips, beats, out_path, 0.0, audio_path, True, start_beat,
                            )
                            if result:
                                engine_url = f"{PUBLIC_BASE_URL}/{out_path}"
                                break
                            print(f"Engine montage attempt {attempt + 1} failed for {job_id}; retrying...")
                    for c in local_clips:  # tidy up the downloaded source clips
                        if c.startswith("uploads/") and os.path.exists(c):
                            try:
                                os.remove(c)
                            except OSError:
                                pass
            except Exception as e:
                print(f"Engine beat-sync failed, falling back to Python path: {e}")

        if engine_url:
            update_job("completed", 100, engine_url)
            print(f"Beat sync job {job_id} completed via engine!")
            return

        # Fallback: Python/ffmpeg montage (silent) + Topaz HD upscale.
        beat_grid = await analyze_audio_beats(request.song_id, request.clip_urls)
        update_job("compositing", 50)
        composed_video_url = await compose_final_video(
            job_id=job_id,
            clip_urls=request.clip_urls,
            beat_data=beat_grid,
            song_id=request.song_id
        )
        final_video_url = await upscale_video(composed_video_url, on_progress=on_fal_progress)
        update_job("completed", 100, final_video_url)
        print(f"Beat sync job {job_id} completed (fallback path)!")
        
    except Exception as e:
        print(f"Job {job_id} failed: {str(e)}")
        db_update_job(job_id, status="failed", progress=0, error=str(e)[:500])

@app.get("/api/v1/jobs")
async def get_all_jobs(user_id: Optional[str] = None):
    # Scope to a single user when user_id is given (the app always passes it).
    return db_get_all_jobs(user_id)

@app.get("/api/v1/user/{user_id}/account")
async def get_user_account(user_id: str):
    # Credits + subscription tier + trial state for the profile/paywall UI.
    return credits.get_account(user_id)

@app.get("/api/v1/user/{user_id}/stats")
async def get_user_stats(user_id: str):
    return db_get_user_stats(user_id)

@app.post("/api/v1/revenuecat/webhook")
async def revenuecat_webhook(request: Request):
    # Verify webhook secret if configured
    secret = os.environ.get("REVENUECAT_WEBHOOK_SECRET", "")
    if secret:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = body.get("event", {})
    event_id = event.get("id")
    event_type = event.get("type")
    app_user_id = event.get("app_user_id")
    product_id = event.get("product_id", "")
    entitlement_ids = event.get("entitlement_ids", [])

    if not event_id or not event_type or not app_user_id:
        raise HTTPException(status_code=400, detail="Missing required webhook event parameters")

    print(f"RevenueCat Webhook Received: id={event_id}, type={event_type}, user={app_user_id}, product={product_id}")

    # Idempotency and logging in Supabase (if enabled)
    if credits.enabled():
        try:
            # Check duplicate
            dup = supabase.table("revenuecat_events").select("id").eq("id", event_id).execute()
            if dup.data and len(dup.data) > 0:
                print(f"Duplicate RevenueCat event ignored: {event_id}")
                return {"status": "success", "message": "Duplicate event ignored"}

            # Try to log event
            # Ensure app_user_id is a valid UUID to write to foreign key user_id
            user_uuid = None
            try:
                user_uuid = str(uuid.UUID(app_user_id))
            except ValueError:
                pass

            supabase.table("revenuecat_events").insert({
                "id": event_id,
                "user_id": user_uuid,
                "event_type": event_type,
                "payload": event
            }).execute()

            # Process database updates based on event type
            if event_type in ("INITIAL_PURCHASE", "RENEWAL"):
                # Determine tier and credits to add
                # E.g. Pro subscription grants 20 credits, Creator grants 8
                refill_credits = 0
                subscription_tier = "free"
                
                # Check product_id or entitlements
                is_pro = "pro" in product_id or "pro" in entitlement_ids
                is_creator = "creator" in product_id or "creator" in entitlement_ids

                if is_pro:
                    subscription_tier = "pro"
                    refill_credits = 20
                elif is_creator:
                    subscription_tier = "creator"
                    refill_credits = 8

                if refill_credits > 0:
                    # Fetch current user credits first
                    user_res = supabase.table("users").select("credits_balance").eq("id", user_uuid).execute()
                    current_credits = 0
                    if user_res.data and len(user_res.data) > 0:
                        current_credits = user_res.data[0].get("credits_balance", 0)

                    supabase.table("users").update({
                        "subscription_tier": subscription_tier,
                        "credits_balance": current_credits + refill_credits,
                        "subscription_renews_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", user_uuid).execute()
                    print(f"Updated user {user_uuid} subscription to {subscription_tier} and added {refill_credits} credits.")

            elif event_type == "NON_SUBSCRIPTION_PURCHASE":
                # Credit top-ups
                credits_to_add = 0
                if "pack" in product_id or "top_up" in product_id or "credit" in product_id:
                    credits_to_add = 10 # Default top-up pack size

                if credits_to_add > 0:
                    user_res = supabase.table("users").select("credits_balance").eq("id", user_uuid).execute()
                    current_credits = 0
                    if user_res.data and len(user_res.data) > 0:
                        current_credits = user_res.data[0].get("credits_balance", 0)

                    supabase.table("users").update({
                        "credits_balance": current_credits + credits_to_add
                    }).eq("id", user_uuid).execute()
                    print(f"Added {credits_to_add} top-up credits to user {user_uuid}.")

            elif event_type in ("CANCELLATION", "EXPIRATION"):
                # Downgrade user to free tier
                supabase.table("users").update({
                    "subscription_tier": "free"
                }).eq("id", user_uuid).execute()
                print(f"Downgraded user {user_uuid} to free tier due to {event_type}.")

        except Exception as e:
            print(f"Error processing RevenueCat webhook in Supabase: {e}")
            raise HTTPException(status_code=500, detail="Database write error")
            
    else:
        # Mock/local fallback mode
        print("Running in Dev/Mock mode. Webhook processed silently.")
        
    return {"status": "success", "message": "Event processed successfully"}

@app.delete("/api/v1/user/{user_id}")
async def delete_user_account(user_id: str):
    print(f"Account deletion request received for user: {user_id}")
    
    if not credits.enabled():
        # Dev/mock mode fallback
        to_delete = []
        for k, v in list(mock_db.items()):
            if v.get("user_id") == user_id:
                to_delete.append(k)
        for k in to_delete:
            del mock_db[k]
        save_mock_db()
        print(f"Mock mode: User {user_id} and associated jobs cleared from memory.")
        return {"status": "success", "message": "User deleted (mock mode)"}

    try:
        user_uuid = str(uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    # 1. Verify user profile exists first
    try:
        user_res = supabase.table("users").select("id").eq("id", user_uuid).execute()
        if not user_res.data or len(user_res.data) == 0:
            raise HTTPException(status_code=404, detail="User not found")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error checking user existence: {e}")
        raise HTTPException(status_code=500, detail="Database check failed")

    try:
        # 2. Delete user's render jobs first to satisfy foreign keys
        supabase.table("render_jobs").delete().eq("user_id", user_uuid).execute()

        # 3. Delete user's logged RevenueCat events
        try:
            supabase.table("revenuecat_events").delete().eq("user_id", user_uuid).execute()
        except Exception as e:
            # Gracefully handle if table does not exist (pre-migration)
            print(f"RevenueCat events table cleanup skipped or failed: {e}")

        # 4. Delete user's profile row in public.users
        supabase.table("users").delete().eq("id", user_uuid).execute()

        # 5. Delete user from auth.users (requires service_role admin privileges)
        supabase.auth.admin.delete_user(user_uuid)

        print(f"User {user_uuid} deleted successfully from database and auth.")
        return {"status": "success", "message": "Account deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting user {user_uuid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}")










