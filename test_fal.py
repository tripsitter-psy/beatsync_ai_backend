import asyncio
import fal_client
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    file_path = "/Users/tripsitter/Documents/beatsync_ai/assets/videos/example_video_small_trimmed.mp4"
    print(f"Uploading {file_path}...")
    try:
        url = await fal_client.upload_file_async(file_path)
        print(f"Success: {url}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(main())
