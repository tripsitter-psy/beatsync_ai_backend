import asyncio
import fal_client
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    video_url = "https://v3b.fal.media/files/b/0a9f4223/8AT-ngSGrhqHXeBf4SZ-n_example_video_small_trimmed.mp4"
    print("Submitting job...")
    try:
        handler = await fal_client.submit_async(
            "fal-ai/hunyuan-video/video-to-video",
            arguments={
                "video_url": video_url,
                "prompt": "test prompt",
                "strength": 0.65,
                "aspect_ratio": "9:16",
            }
        )
        print("Waiting for events...")
        async for event in handler.iter_events(with_logs=True):
            print(event)
        result = await handler.get()
        print("Success!", result)
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(main())
