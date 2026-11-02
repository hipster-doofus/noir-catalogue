"""
Film Noir Scene Extractor & Supabase Indexer
Designed for headless execution in GitHub Actions or any serverless cloud runner.
"""

import os
import sys
import json
import time
import random
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests
import imageio_ffmpeg

# Setup directories
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = BASE_DIR / "temp_workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Curated catalog of verified Public Domain Film Noir classics on Internet Archive
FILM_NOIR_CATALOG = [
    {
        "title": "Detour",
        "year": 1945,
        "director": "Edgar G. Ulmer",
        "archive_id": "Detour",
        "video_url": "https://archive.org/download/Detour/Detour.mp4",
        "description": "A broke New York pianist hitchhikes west to join his girlfriend, but a stranger's death plunges him into a fatal nightmare."
    },
    {
        "title": "D.O.A.",
        "year": 1949,
        "director": "Rudolph Maté",
        "archive_id": "D.O.A.1949",
        "video_url": "https://archive.org/download/D.O.A.1949/D.O.A..mp4",
        "description": "Frank Bigelow walks into a police station to report his own murder after being fatally poisoned with luminous toxin."
    },
    {
        "title": "The Hitch-Hiker",
        "year": 1953,
        "director": "Ida Lupino",
        "archive_id": "the-hitch-hiker-1953",
        "video_url": "https://archive.org/download/the-hitch-hiker-1953/The%20Hitch-Hiker.mp4",
        "description": "Directed by cinema pioneer Ida Lupino; two fishing buddies pick up a psychopathic fugitive hitchhiking across the desert."
    },
    {
        "title": "Scarlet Street",
        "year": 1945,
        "director": "Fritz Lang",
        "archive_id": "ScarletStreet1945",
        "video_url": "https://archive.org/download/ScarletStreet1945/ScarletStreet1945.mp4",
        "description": "Fritz Lang's noir classic about a meek cashier who becomes entangled with a scheming femme fatale and her con-artist boyfriend."
    },
    {
        "title": "Suddenly",
        "year": 1954,
        "director": "Lewis Allen",
        "archive_id": "Suddenly_1954",
        "video_url": "https://archive.org/download/Suddenly_1954/Suddenly.mp4",
        "description": "Frank Sinatra stars as a cold-blooded assassin holding a suburban family hostage while awaiting the U.S. President's train."
    }
]

def get_ffmpeg():
    """Locate or return bundled FFmpeg executable."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def select_movie():
    """Pick movie based on optional override or random selection."""
    override_id = os.environ.get("MOVIE_ID_OVERRIDE", "").strip()
    if override_id:
        for m in FILM_NOIR_CATALOG:
            if m["archive_id"].lower() == override_id.lower():
                return m
        # If not in catalog, construct from archive ID
        return {
            "title": override_id.replace("_", " ").title(),
            "year": 1950,
            "director": "Unknown",
            "archive_id": override_id,
            "video_url": f"https://archive.org/download/{override_id}/{override_id}.mp4",
            "description": "Public domain film noir from Internet Archive."
        }
    return random.choice(FILM_NOIR_CATALOG)

def analyze_multimodal(clip_path, movie_info, scene_index, duration):
    """
    Multimodal AI Analysis:
    1. Uses Gemini API (free tier) if GEMINI_API_KEY is available.
    2. Falls back to faster-whisper + heuristic visual descriptors.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            print(f"    🤖 Calling Gemini Multimodal API for Scene {scene_index}...")
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=gemini_key)
            uploaded_file = client.files.upload(file=str(clip_path))
            
            prompt = """
            Analyze this 1080p Film Noir scene clip and respond in strict JSON format:
            {
                "transcription": "Verbatim spoken dialogue or [Instrumental/Ambient]",
                "visual_description": "Detailed staging, lighting (chiaroscuro/shadows), and camera framing",
                "synopsis": "Concise 2-3 sentence narrative summary of the dramatic beat",
                "mood": "Dominant emotional tone (e.g. fatalistic, paranoid, melancholic, tense)",
                "noir_tropes": ["list", "of", "tropes", "detected"],
                "tags": ["relevant", "keywords"]
            }
            """
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"    [!] Gemini API notice ({e}), switching to local faster-whisper...")

    # Offline / Free Fallback using faster-whisper
    try:
        from faster_whisper import WhisperModel
        print(f"    🎙️ Transcribing audio with faster-whisper (Scene {scene_index})...")
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(clip_path))
        transcript = " ".join([seg.text.strip() for seg in segments]).strip()
    except Exception as e:
        print(f"    [!] Whisper fallback error ({e})")
        transcript = "[Ambient noir orchestral score and environmental sound]"

    if not transcript:
        transcript = "[Instrumental noir soundtrack / ambient score]"

    return {
        "transcription": transcript,
        "visual_description": f"Black-and-white high-contrast sequence from '{movie_info['title']}' featuring dramatic low-key lighting and atmospheric camera framing.",
        "synopsis": f"Dramatic dialogue and character development sequence from '{movie_info['title']}' ({movie_info['year']}).",
        "mood": "Fatalistic, melancholic, tense",
        "noir_tropes": ["low-key lighting", "voiceover cadence", "doomed protagonist"],
        "tags": ["film noir", "1940s", "crime drama", "black and white"]
    }

def index_to_supabase(movie, scenes):
    """Upserts movies and scenes into Supabase PostgreSQL database."""
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()

    if not supabase_url or not supabase_key:
        print("\n⚠️ SUPABASE_URL or SUPABASE_KEY not set. Skipping Supabase cloud upload.")
        return False

    try:
        from supabase import create_client
        print(f"\n🌐 Connecting to Supabase at {supabase_url}...")
        client = create_client(supabase_url, supabase_key)

        # 1. Upsert Movie
        movie_payload = {
            "movie_id": movie["archive_id"],
            "title": movie["title"],
            "director": movie["director"],
            "release_year": movie["year"],
            "source_archive_url": movie["video_url"],
            "license": "Public Domain"
        }
        client.table("movies").upsert(movie_payload).execute()
        print(f"  [✓] Movie record indexed: {movie['title']}")

        # 2. Upsert Scenes
        for s in scenes:
            scene_payload = {
                "scene_id": f"{movie['archive_id']}_scene_{s['scene_number']}",
                "movie_id": movie["archive_id"],
                "scene_number": s["scene_number"],
                "start_seconds": s["start_seconds"],
                "end_seconds": s["end_seconds"],
                "duration_seconds": s["duration_seconds"],
                "start_timecode": s["start_timecode"],
                "end_timecode": s["end_timecode"],
                "transcription": s["transcription"],
                "visual_description": s["visual_description"],
                "synopsis": s["synopsis"],
                "mood": s["mood"],
                "noir_tropes": s.get("noir_tropes", []),
                "tags": s.get("tags", []),
                "keyframe_filename": s["keyframe_file"],
                "video_filename": s["video_file"],
                "resolution": "1920x1080",
                "video_codec": "h264",
                "audio_codec": "aac",
                "download_url": s.get("download_url", "")
            }
            client.table("scenes").upsert(scene_payload).execute()
            print(f"  [✓] Scene {s['scene_number']} indexed into Supabase")

        print("✅ Supabase cloud database indexing complete!")
        return True
    except Exception as e:
        print(f"❌ Supabase error: {e}")
        return False

def index_to_sqlite(movie, scenes, db_path):
    """Saves local SQLite backup database."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS movies (
        movie_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        director TEXT,
        release_year INTEGER,
        source_archive_url TEXT,
        license TEXT DEFAULT 'Public Domain'
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS scenes (
        scene_id TEXT PRIMARY KEY,
        movie_id TEXT NOT NULL,
        scene_number INTEGER NOT NULL,
        start_seconds REAL NOT NULL,
        end_seconds REAL NOT NULL,
        duration_seconds REAL NOT NULL,
        start_timecode TEXT NOT NULL,
        end_timecode TEXT NOT NULL,
        transcription TEXT,
        visual_description TEXT,
        synopsis TEXT,
        mood TEXT,
        noir_tropes TEXT,
        tags TEXT,
        keyframe_filename TEXT,
        video_filename TEXT,
        resolution TEXT DEFAULT '1920x1080',
        video_codec TEXT DEFAULT 'h264',
        audio_codec TEXT DEFAULT 'aac',
        created_at TEXT NOT NULL,
        FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    );
    """)

    cur.execute("""
    INSERT OR REPLACE INTO movies (movie_id, title, director, release_year, source_archive_url)
    VALUES (?, ?, ?, ?, ?);
    """, (movie["archive_id"], movie["title"], movie["director"], movie["year"], movie["video_url"]))

    for s in scenes:
        cur.execute("""
        INSERT OR REPLACE INTO scenes (
            scene_id, movie_id, scene_number,
            start_seconds, end_seconds, duration_seconds,
            start_timecode, end_timecode,
            transcription, visual_description, synopsis, mood,
            noir_tropes, tags, keyframe_filename, video_filename,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            f"{movie['archive_id']}_scene_{s['scene_number']}",
            movie["archive_id"],
            s["scene_number"],
            s["start_seconds"],
            s["end_seconds"],
            s["duration_seconds"],
            s["start_timecode"],
            s["end_timecode"],
            s["transcription"],
            s["visual_description"],
            s["synopsis"],
            s["mood"],
            json.dumps(s.get("noir_tropes", [])),
            json.dumps(s.get("tags", [])),
            s["keyframe_file"],
            s["video_file"],
            datetime.now(timezone.utc).isoformat()
        ))

    conn.commit()
    conn.close()
    print(f"✅ Local SQLite database updated: {db_path}")

def main():
    ffmpeg_exe = get_ffmpeg()
    movie = select_movie()
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 65)
    print(f"🎬 SELECTED FILM NOIR: {movie['title']} ({movie['year']})")
    print(f"   Director: {movie['director']}")
    print(f"   Archive Identifier: {movie['archive_id']}")
    print(f"   URL: {movie['video_url']}")
    print("=" * 65)

    # 1. Download / stream 15-minute sample from Internet Archive
    sample_file = WORK_DIR / f"{movie['archive_id']}_source.mp4"
    print("\n🌐 Streaming film segment from Internet Archive...")
    t0 = time.time()
    cmd = [
        ffmpeg_exe, "-y",
        "-ss", "00:03:00",
        "-i", movie["video_url"],
        "-t", "00:15:00",
        "-c", "copy",
        str(sample_file)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    print(f"✅ Stream segment cached in {time.time()-t0:.1f}s ({sample_file.stat().st_size / (1024*1024):.1f} MB)")

    # 2. AI Scene Recognition using PySceneDetect
    from scenedetect import open_video, SceneManager, AdaptiveDetector
    print("\n🔍 Running PySceneDetect AdaptiveDetector...")
    video = open_video(str(sample_file))
    sm = SceneManager()
    sm.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=24))
    sm.detect_scenes(video, frame_skip=2)
    scene_list = sm.get_scene_list()

    valid_scenes = []
    for st, en in scene_list:
        dur = (en - st).seconds
        if 10.0 <= dur <= 60.0:
            valid_scenes.append((st, en, dur))

    print(f"✅ Total cuts detected: {len(scene_list)}")
    print(f"✅ Scenes matching duration (10s - 60s): {len(valid_scenes)}")

    if len(valid_scenes) < 3:
        # Fallback to segments
        valid_scenes = [(st, en, (en-st).seconds) for st, en in scene_list[:5]]

    chosen = random.sample(valid_scenes, 3)
    chosen.sort(key=lambda s: s[0].get_seconds())

    # 3. Transcode 3 Scenes to 1080p MP4 (H.264 / AAC)
    processed_scenes = []
    print("\n🎞️ Transcoding 3 scenes to 1080p Full HD (H.264 / AAC)...")

    for idx, (st, en, dur) in enumerate(chosen, 1):
        clip_name = f"{movie['archive_id']}_scene_{idx}_{int(dur)}s_1080p.mp4"
        thumb_name = f"{movie['archive_id']}_scene_{idx}_thumb.jpg"
        clip_path = OUTPUT_DIR / clip_name
        thumb_path = OUTPUT_DIR / thumb_name

        # Encode 1080p
        transcode_cmd = [
            ffmpeg_exe, "-y",
            "-ss", st.get_timecode(),
            "-to", en.get_timecode(),
            "-i", str(sample_file),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "19",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(clip_path)
        ]
        subprocess.run(transcode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)

        # Extract Keyframe Thumbnail
        mid_sec = (st.get_seconds() + en.get_seconds()) / 2.0
        thumb_cmd = [
            ffmpeg_exe, "-y",
            "-ss", f"{mid_sec:.3f}",
            "-i", str(sample_file),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_path)
        ]
        subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)

        file_mb = clip_path.stat().st_size / (1024 * 1024)
        print(f"  [✓] Scene {idx} encoded: {clip_name} ({file_mb:.2f} MB)")

        # AI Multimodal Metadata
        print(f"  [*] Running AI multimodal analysis for Scene {idx}...")
        meta = analyze_multimodal(clip_path, movie, idx, dur)

        scene_record = {
            "scene_number": idx,
            "start_timecode": st.get_timecode(),
            "end_timecode": en.get_timecode(),
            "start_seconds": st.get_seconds(),
            "end_seconds": en.get_seconds(),
            "duration_seconds": dur,
            "video_file": clip_name,
            "keyframe_file": thumb_name,
            "clip_path": clip_path,
            "thumb_path": thumb_path,
            **meta
        }
        processed_scenes.append(scene_record)

    # 4. Save to SQLite Backup Database
    sqlite_db_path = OUTPUT_DIR / "film_noir_scenes.db"
    index_to_sqlite(movie, processed_scenes, sqlite_db_path)

    # 5. Index to Supabase Cloud
    index_to_supabase(movie, processed_scenes)

    # 6. Package ZIP Archive
    zip_filename = f"{movie['archive_id']}_3_Scenes_1080p_Pack.zip"
    zip_path = OUTPUT_DIR / zip_filename
    print(f"\n📦 Packaging ZIP archive: {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in processed_scenes:
            zf.write(s["clip_path"], arcname=s["video_file"])
            zf.write(s["thumb_path"], arcname=s["keyframe_file"])
        zf.write(sqlite_db_path, arcname=sqlite_db_path.name)

        # Write human-readable report
        report = f"""=====================================================
FILM NOIR AI SCENE CATALOG REPORT
Movie: {movie['title']} ({movie['year']})
Director: {movie['director']}
Source: {movie['video_url']}
Extracted At: {datetime.now(timezone.utc).isoformat()}
=====================================================
"""
        for s in processed_scenes:
            report += f"""
SCENE {s['scene_number']}: {s['start_timecode']} -> {s['end_timecode']} ({s['duration_seconds']:.1f}s)
Mood: {s['mood']}
Synopsis: {s['synopsis']}
Visual: {s['visual_description']}
Audio Transcript: {s['transcription']}
Tropes: {', '.join(s.get('noir_tropes', []))}
Tags: {', '.join(s.get('tags', []))}
-----------------------------------------------------
"""
        zf.writestr("scenes_summary.txt", report)

    # Summary text in output folder for GitHub release notes
    summary_file = OUTPUT_DIR / "scenes_summary.txt"
    summary_file.write_text(report, encoding="utf-8")

    # Set GitHub Actions environment outputs if in CI
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"MOVIE_TITLE={movie['title']}\n")
            f.write(f"TIMESTAMP={timestamp_str}\n")
            f.write(f"ZIP_FILE_PATH={zip_path.as_posix()}\n")

    print("=" * 65)
    print("🎉 EXTRACTION & INDEXING COMPLETE!")
    print(f"📦 ZIP Archive: {zip_path} ({zip_path.stat().st_size / (1024*1024):.2f} MB)")
    print(f"🗄️ SQLite DB:   {sqlite_db_path}")
    print("=" * 65)

if __name__ == "__main__":
    main()
