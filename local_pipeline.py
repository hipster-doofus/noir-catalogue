#!/usr/bin/env python3
"""
Noir Vault – Local Directory Scene Extraction Pipeline
Harvests random scene clips from a local folder of movie files.
"""

import os
import random
import re
import subprocess
from pathlib import Path

# Paths
INPUT_DIR = Path("./local_movies")    # Drop your source MP4/MKV files here
OUTPUT_DIR = Path("./output_clips")   # Where extracted clips will be saved

def get_video_duration(video_path: Path) -> float:
    """Gets total duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

def extract_local_clip(video_path: Path, start_sec: float, duration_sec: float, dest_path: Path) -> bool:
    """Extracts a clip with audio volume normalization and reset timecodes."""
    cmd = [
        "ffmpeg", "-y",
        "-avoid_negative_ts", "make_zero",
        "-ss", str(start_sec),
        "-i", str(video_path),
        "-t", str(duration_sec),
        "-fflags", "+genpts",
        "-vf", "scale='if(lt(ih,1080),-2,iw)':'if(lt(ih,1080),1080,ih)':force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dest_path)
    ]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode == 0

def process_local_library(num_clips_per_movie=5, clip_duration=45):
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    video_files = [f for f in INPUT_DIR.iterdir() if f.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov']]
    if not video_files:
        print(f"❌ No videos found in {INPUT_DIR.resolve()}. Place video files there first!")
        return

    for video_file in video_files:
        print(f"\n🎬 Harvesting clips from local file: {video_file.name}")
        total_duration = get_video_duration(video_file)
        
        if total_duration < 300: # Skip clips under 5 mins
            print(f"  ⚠ Skipping {video_file.name} (too short)")
            continue

        clean_name = re.sub(r"[^A-Za-z0-9_-]", "_", video_file.stem)[:30]
        
        # Pick random start times across the movie (ignoring first/last 5 minutes)
        min_start = 300
        max_start = int(total_duration - clip_duration - 300)
        
        for i in range(1, num_clips_per_movie + 1):
            start_time = random.randint(min_start, max_start)
            clip_name = f"{clean_name}_scene_{i:02d}_{start_time}s.mp4"
            dest_clip = OUTPUT_DIR / clip_name

            print(f"  🎞 Extracting scene {i} starting at {start_time}s...")
            if extract_local_clip(video_file, start_time, clip_duration, dest_clip):
                print(f"    [✓] Saved: {clip_name}")

if __name__ == "__main__":
    process_local_library()
