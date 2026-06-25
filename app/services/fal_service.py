import os
import fal_client
import asyncio
import logging
import re
import uuid
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Public base URL of THIS backend. Files we serve (uploads/, songs/) are local
# on disk; an incoming video_url that starts with this is one of ours and gets
# resolved to a local path. Must match the app's API_BASE_URL host in prod.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://192.168.1.169:8000")


def _local_path_for(url: str) -> str | None:
    """If `url` is served by this backend, return its local file path; else None
    (e.g. an external fal.ai URL that should be left untouched)."""
    for folder in ["uploads", "songs"]:
        if f"/{folder}/" in url:
            idx = url.index(f"/{folder}/")
            return url[idx:].lstrip("/")
    return None

async def extract_and_stylize_first_frame(video_url: str, prompt: str, on_progress=None) -> str:
    """
    Extracts the first frame of the video, uploads it, and uses Flux Image-to-Image
    to stylize it based on the prompt. Returns the URL of the stylized image.
    """
    try:
        if on_progress:
            await on_progress("extracting_frame", 10) # Notify we are extracting

        # 1. Extract first frame if it's a local video
        local_path = _local_path_for(video_url) or video_url
        
        # If the video_url is external, we'd need to download it first. 
        # But for this app, videos are always uploaded locally.
        if not os.path.exists(local_path):
            logger.warning(f"Local file {local_path} not found for frame extraction.")
            return None

        extracted_frame_path = f"uploads/{uuid.uuid4()}_frame.jpg"
        logger.info(f"Extracting first frame from {local_path} to {extracted_frame_path}...")
        
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", local_path, "-vframes", "1", "-y", extracted_frame_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()

        if not os.path.exists(extracted_frame_path):
            logger.error("Failed to extract frame.")
            return None

        # 2. Upload extracted frame to fal
        if on_progress:
            await on_progress("stylizing_frame", 20) # Notify we are stylizing
            
        logger.info("Uploading extracted frame to fal.ai...")
        frame_url = await fal_client.upload_file_async(extracted_frame_path)

        # 3. Stylize with FireRed Image Edit v1.1
        logger.info(f"Stylizing frame with FireRed Image Edit: {prompt}")
        handler = await fal_client.submit_async(
            "fal-ai/firered-image-edit-v1.1",
            arguments={
                "image_urls": [frame_url],
                "prompt": prompt,
            }
        )
        
        result = await handler.get()
        if "images" in result and len(result["images"]) > 0:
            stylized_url = result["images"][0]["url"]
            logger.info(f"Successfully stylized frame: {stylized_url}")
            return stylized_url
            
    except Exception as e:
        logger.error(f"Error in extract_and_stylize_first_frame: {e}")
        
    return None

prompts = {
    "Mecha Robot": "A high-quality 3D mecha robot animation, giant mechanized suit, glowing neon energy cores, metallic armor plating, heavy footsteps, cinematic lighting, 8k resolution, masterpiece.",
    "Anime Hero": "A high-quality 2D anime style animation, vibrant colors, Studio Ghibli style, detailed cel shading, magical aura, dynamic action pose, beautifully drawn eyes, masterpiece.",
    "Pixel Legend": "16-bit retro pixel art style, detailed sprite animation, vibrant arcade colors, glowing pixelated aura, nostalgic 90s video game aesthetic.",
    "Cyberpunk Runner": "Cyberpunk aesthetic, highly detailed futuristic runner, glowing neon cybernetics, reflections in puddles, neon outlines, high contrast, gritty realism.",
    "Dark Knight": "Dark fantasy knight in heavy gothic armor, glowing red eyes, dramatic shadows, moody lighting, highly detailed metal reflections, epic fantasy masterpiece.",
    "Spirit Animal": "Ethereal glowing spirit animal, translucent magical aura, floating particles, neon bioluminescence, enchanting and beautiful animation.",
    "Liquid Chrome": "Liquid metal chrome aesthetic, highly reflective fluid motion, mercury-like body, abstract and surreal, morphing shapes, studio lighting, hyper-realistic reflections, 4k.",
    "Custom Style": "A high quality digital art animation, creative and unique, masterpiece, dynamic movement, beautiful lighting."
}

# User explicitly requested a human skeleton prompt for mock_style_id
skeleton_prompt = "A high quality video of a dancing human skeleton, photorealistic bone structure, glowing neon internal energy, eerie but cool aesthetic, detailed ribs and skull, cinematic lighting, 4k resolution, masterpiece."

# Niche prop names the model doesn't recognise (e.g. "poi") get expanded into a
# physical description so the model actually renders the object, not the word.
# Keys are matched case-insensitively as substrings of the user's props text.
PROP_DESCRIPTIONS = {
    "poi": (
        "a pair of ropes, one held in each hand, each with a small glowing ball "
        "of electric plasma dangling from the end of the rope, swinging and "
        "trailing vibrant light as they spin"
    ),
    "fire staff": (
        "a long staff held in both hands with both ends wrapped in vibrant, "
        "glowing flames, trailing fire as it spins"
    ),
    "light saber": (
        "a metal hilt projecting a long glowing blade of vibrant energy"
    ),
    "lightsaber": (
        "a metal hilt projecting a long glowing blade of vibrant energy"
    ),
}


def describe_props(props: str) -> str:
    """Expand known niche prop names into a physical description; otherwise
    return the user's text unchanged."""
    text = props.strip()
    lowered = text.lower()
    for key, description in PROP_DESCRIPTIONS.items():
        if key in lowered:
            return description
    return text


def build_prompt(style_id: str, props: str = None) -> str:
    """Build the full fal prompt for a style + optional props. Shared by the
    preview endpoint and the render so both send identical text."""
    base_prompt = prompts.get(style_id, style_id)
    prompt = base_prompt + (
        ", exactly tracking the original human movement, humanoid figure, solo, "
        "full body, continuous single take, no scene cuts, seamless motion."
    )
    if props and props.strip():
        described = describe_props(props)
        prompt += (
            f" The character is fluidly dancing with and holding {described}. "
            "These props are heavily integrated into the movement and glow vibrantly."
        )
    return prompt


def build_reference_prompt(style_id: str, props: str = None) -> str:
    """Build the firered prompt for the SCAIL-2 *character reference* image.

    SCAIL-2 runs a subject detector on image_url and rejects scene-embedded
    images ("no subject detected"), so the reference must be a single, clearly
    visible character on a clean background — NOT the original scene.
    """
    base_prompt = prompts.get(style_id, style_id)
    prompt = base_prompt + (
        " A single centered clearly visible character on a clean plain neutral studio background, "
        "sharp and well-lit, masterpiece."
    )
    if props and props.strip():
        described = describe_props(props)
        prompt += f" The character is holding {described}."
    return prompt


async def run_scail2_animation(video_url: str, style_id: str, mode: str, props: str = None, preview_image_url: str = None, on_progress=None) -> str:
    """
    Submits a video to fal.ai for video-to-video style transfer.
    If preview_image_url is provided, it uses it directly instead of re-stylizing.
    Returns the resulting animated video URL.
    """
    api_key = os.getenv("FAL_KEY")
    if not api_key:
        logger.warning("FAL_KEY not set. Running in simulation mode.")
        await asyncio.sleep(5)
        return "https://mock.url/fal_scail2_output.mp4"

    try:
        # Two prompts: a descriptive one for SCAIL-2 (the final video) and a
        # clean-character one for the firered reference image (which SCAIL-2's
        # subject detector requires to be an isolated, full-body character).
        prompt = build_prompt(style_id, props)
        reference_prompt = build_reference_prompt(style_id, props)

        # 1. Pre-style the first frame BEFORE uploading the video to fal.
        # extract_and_stylize_first_frame needs the local file, so it must run
        # while video_url still points at our local server (not a fal URL).
        if preview_image_url:
            stylized_image_url = preview_image_url
            logger.info(f"Using provided preview image URL: {stylized_image_url}")
        else:
            stylized_image_url = await extract_and_stylize_first_frame(video_url, reference_prompt, on_progress=on_progress)

        # 2. Now upload the local video file to fal.ai storage if it's hosted locally
        file_path = _local_path_for(video_url)
        if file_path is not None:
            if os.path.exists(file_path):
                logger.info(f"Uploading local file {file_path} to fal.ai...")
                video_url = await fal_client.upload_file_async(file_path)
            else:
                logger.error(f"Local file {file_path} not found!")

        logger.info(f"Submitting job to fal.ai (SCAIL-2) with prompt: {prompt}")

        # SCAIL-2 (Wan2.1 character animation): drives the reference character
        # (image_url) with the motion from the driving video (video_url).
        #   mode="replacement": swap the subject in the driving video for the
        #     reference character, keeping the real background + motion + props.
        #   mode="animation": animate the reference image itself with the motion.
        args = {
            "prompt": prompt,
            "video_url": video_url,                     # driving motion (your clip)
            "mode": os.getenv("SCAIL2_MODE", "replacement"),
            "subject_type": "human",
            "resolution": os.getenv("SCAIL2_RESOLUTION", "704p"),
        }

        # The stylized still is the reference character SCAIL-2 animates — this is
        # what actually re-skins the subject (hunyuan v2v ignored it).
        if stylized_image_url:
            args["image_url"] = stylized_image_url
        else:
            logger.warning("No reference image for SCAIL-2; output may not transform the subject.")

        # 3. Call the SCAIL-2 endpoint
        handler = await fal_client.submit_async(
            "fal-ai/scail-2",
            arguments=args
        )

        # Track render progress. Log formats vary, so parse the common
        # ones ("x/y" or "z%") and fall back to a heartbeat that nudges the bar
        # forward on every event so the UI never appears frozen.
        last_percent = 20
        async for event in handler.iter_events(with_logs=True):
            if not isinstance(event, fal_client.InProgress):
                continue

            percent = None
            for log in (event.logs or []):
                msg = log.get("message", "")
                frac = re.search(r'(\d+)\s*/\s*(\d+)', msg)
                pct = re.search(r'(\d+(?:\.\d+)?)\s*%', msg)
                if frac and int(frac.group(2)) > 0:
                    percent = 20 + int(int(frac.group(1)) / int(frac.group(2)) * 78)
                elif pct:
                    percent = 20 + int(float(pct.group(1)) / 100 * 78)

            # No parseable number this event: creep toward 95% so it keeps moving.
            if percent is None:
                percent = min(last_percent + 2, 95)

            if percent > last_percent and on_progress:
                last_percent = percent
                await on_progress("rendering_video", percent)

        result = await handler.get()
        video_url_out = (result.get("video") or {}).get("url")
        if not video_url_out:
            raise RuntimeError(f"SCAIL-2 returned no video url: {result}")
        return video_url_out

    except fal_client.client.FalClientHTTPError as e:
        # Surface fal validation errors (e.g. "no subject detected in the
        # reference image") so the job fails with a useful message instead of
        # hanging or dying silently.
        logger.error(f"SCAIL-2 rejected the request: {e}")
        raise
    except Exception as e:
        logger.error(f"Error calling fal.ai: {e}")
        raise


async def interpolate_with_rife(video_url: str, on_progress=None) -> str:
    """Smooth a SCAIL-2 clip (16fps) by interpolating frames with RIFE.

    Returns the interpolated video URL. On any failure this returns the original
    url unchanged — interpolation is a polish step and must never fail the job.
    """
    if not os.getenv("FAL_KEY"):
        return video_url
    try:
        if on_progress:
            await on_progress("interpolating", 96)
        # num_frames=1 inserts one frame between each pair -> ~2x fps (16 -> ~32).
        multiplier = int(os.getenv("RIFE_NUM_FRAMES", "1"))
        handler = await fal_client.submit_async(
            "fal-ai/rife/video",
            arguments={
                "video_url": video_url,
                "num_frames": multiplier,
                "use_calculated_fps": True,
            },
        )
        result = await handler.get()
        smoothed = (result.get("video") or {}).get("url")
        if smoothed:
            logger.info(f"RIFE interpolation done: {smoothed}")
            return smoothed
        logger.warning(f"RIFE returned no url, keeping original. {result}")
    except Exception as e:
        logger.error(f"RIFE interpolation failed, keeping original clip: {e}")
    return video_url


async def upscale_video(video_url: str, on_progress=None) -> str:
    """HD-upscale a clip with Topaz (used for the final beat-synced export).

    SCAIL-2 renders at 704p; this lifts the committed beat-sync output toward
    ~1080p. Returns the original url unchanged on any failure (never blocks).
    target_fps is intentionally left unset — the clip is already RIFE-smoothed,
    so we only upscale resolution here (and avoid Topaz's 60fps price-doubling).
    """
    if not os.getenv("FAL_KEY"):
        return video_url
    try:
        if on_progress:
            await on_progress("upscaling", 85)
        factor = float(os.getenv("UPSCALE_FACTOR", "1.5"))  # 704p -> ~1080p
        handler = await fal_client.submit_async(
            "fal-ai/topaz/upscale/video",
            arguments={
                "video_url": video_url,
                "upscale_factor": factor,
                "H264_output": True,  # broadest phone / social compatibility
            },
        )
        result = await handler.get()
        upscaled = (result.get("video") or {}).get("url")
        if upscaled:
            logger.info(f"Topaz upscale done: {upscaled}")
            return upscaled
        logger.warning(f"Upscale returned no url, keeping original. {result}")
    except Exception as e:
        logger.error(f"Upscale failed, keeping original clip: {e}")
    return video_url
