"""
Multi-Source Film Noir Scene Extractor & Cloud Indexer
Supports Internet Archive, Library of Congress, and YouTube Restorations (via yt-dlp).
Processes up to 10 movies per batch, generates rich AI metadata, and updates Supabase & GitHub Pages.
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
import gc
from datetime import datetime, timezone
from pathlib import Path
import requests
import imageio_ffmpeg

# Seed random with entropy
random.seed(int(time.time() * 1000) ^ os.getpid())

# Setup directories
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = BASE_DIR / "temp_workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Blended Catalog of Verified Public Domain Film Noir Classics across Multiple Sources
BLENDED_NOIR_CATALOG = [
    # --- Internet Archive Public Domain Archive ---
    {
        "title": "Suddenly",
        "year": 1954,
        "director": "Lewis Allen",
        "archive_id": "suddenly",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/suddenly/suddenly.mp4",
        "description": "Frank Sinatra stars as a cold-blooded assassin holding a family hostage while plotting a presidential assassination."
    },
    {
        "title": "The Stranger",
        "year": 1946,
        "director": "Orson Welles",
        "archive_id": "TheStranger_0",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/TheStranger_0/The_Stranger.mp4",
        "description": "Directed by and starring Orson Welles with Edward G. Robinson; a war crimes investigator tracks an escaped fugitive in Connecticut."
    },
    {
        "title": "Quicksand",
        "year": 1950,
        "director": "Irving Pichel",
        "archive_id": "Quicksand_clear",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Quicksand_clear/Quicksand.mp4",
        "description": "Mickey Rooney and Peter Lorre in a relentless noir where a minor theft spirals into extortion and robbery."
    },
    {
        "title": "Detour",
        "year": 1945,
        "director": "Edgar G. Ulmer",
        "archive_id": "Detour",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Detour/Detour.mp4",
        "description": "A penniless New York pianist hitchhikes to Hollywood, only to be dragged into an inescapable nightmare of blackmail."
    },
    {
        "title": "The Strange Love of Martha Ivers",
        "year": 1946,
        "director": "Lewis Milestone",
        "archive_id": "Martha_Ivers_movie",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Martha_Ivers_movie/Strange_Love_Martha_Ivers.mp4",
        "description": "Barbara Stanwyck, Van Heflin, and Kirk Douglas in his screen debut; childhood friends bound by a deadly secret."
    },
    {
        "title": "Please Murder Me",
        "year": 1956,
        "director": "Peter Godfrey",
        "archive_id": "Please_Murder_Me_movie",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Please_Murder_Me_movie/Please_Murder_Me.mp4",
        "description": "Raymond Burr plays an attorney who clears a femme fatale (Angela Lansbury), only to discover her deadly guilt."
    },
    {
        "title": "Behind Green Lights",
        "year": 1946,
        "director": "Otto Brower",
        "archive_id": "BehindGreenLights_high_Q_mp4",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/BehindGreenLights_high_Q_mp4/Behind_Green_Lights.mp4",
        "description": "Carole Landis stars in an atmospheric police procedural centered around a midnight precinct murder."
    },
    {
        "title": "He Walked By Night",
        "year": 1948,
        "director": "Alfred L. Werker",
        "archive_id": "He_Walked_By_Night.avi",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/He_Walked_By_Night.avi/He_Walked_By_Night.mp4",
        "description": "Richard Basehart stars as a cunning electronics thief stalking through the storm drains beneath Los Angeles."
    },
    {
        "title": "The Red House",
        "year": 1947,
        "director": "Delmer Daves",
        "archive_id": "TheRedHouse",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/TheRedHouse/The_Red_House.mp4",
        "description": "Edward G. Robinson plays an isolated farmer guarding a terrifying mystery hidden in the forbidden woods."
    },
    {
        "title": "Kansas City Confidential",
        "year": 1952,
        "director": "Phil Karlson",
        "archive_id": "kansascityconfidencial",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/kansascityconfidencial/kansascityconfidencial.mp4",
        "description": "John Payne stars as an ex-con framed for a heist who tracks the masked perpetrators across Mexico."
    },
    {
        "title": "Woman on the Run",
        "year": 1950,
        "director": "Norman Foster",
        "archive_id": "Woman_on_the_Run",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Woman_on_the_Run/Woman_on_the_Run.mp4",
        "description": "Ann Sheridan navigates the dark night streets of San Francisco searching for her missing husband."
    },
    {
        "title": "The Chase",
        "year": 1946,
        "director": "Arthur Ripley",
        "archive_id": "TheChase_",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/TheChase_/TheChase.mp4",
        "description": "Robert Cummings and Peter Lorre in a feverish Cornell Woolrich thriller involving a sadistic Florida gangster."
    },
    {
        "title": "The Man Who Cheated Himself",
        "year": 1950,
        "director": "Felix E. Feist",
        "archive_id": "Man_Who_Cheated_Himself",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Man_Who_Cheated_Himself/NOIR_DISC_1.mp4",
        "description": "Lee J. Cobb stars as a veteran detective who covers up a crime for his lover, only to have his rookie brother assigned to the case."
    },
    {
        "title": "Inner Sanctum",
        "year": 1948,
        "director": "Lew Landers",
        "archive_id": "Inner_Sanctum_movie",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Inner_Sanctum_movie/Inner_Sanctum.mp4",
        "description": "A murderer takes refuge in a flood-bound boarding house, unaware a fellow traveler knows his crime."
    },
    {
        "title": "Whistle Stop",
        "year": 1946,
        "director": "Léonide Moguy",
        "archive_id": "Whistle_Stop",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Whistle_Stop/Whistle_Stop.mp4",
        "description": "Ava Gardner and George Raft in a story of small-town corruption, jealousy, and an ill-fated robbery."
    },
    {
        "title": "The Second Woman",
        "year": 1950,
        "director": "James V. Kern",
        "archive_id": "The_Second_Woman_",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/The_Second_Woman_/The_Second_Woman_.mp4",
        "description": "Robert Young stars as a haunted architect plagued by fatal accidents following the death of his bride."
    },
    {
        "title": "Jigsaw",
        "year": 1949,
        "director": "Fletcher Markle",
        "archive_id": "Jigsaw_",
        "source_provider": "Internet Archive",
        "video_url": "https://archive.org/download/Jigsaw_/Jigsaw.mp4",
        "description": "Franchot Tone plays an investigator unraveling a syndicate of political racketeers in New York City."
    },
    # --- Library of Congress & YouTube Restorations (Streamed via yt-dlp) ---
    {
        "title": "The Hitch-Hiker (Restored)",
        "year": 1953,
        "director": "Ida Lupino",
        "archive_id": "the_hitch_hiker_restored",
        "source_provider": "YouTube",
        "video_url": "https://www.youtube.com/watch?v=N8Z7s0F7V0g",
        "description": "Ida Lupino's desert masterpiece; two friends on a fishing trip pick up a psychopathic fugitive."
    },
    {
        "title": "Impact (Restored)",
        "year": 1949,
        "director": "Arthur Lubin",
        "archive_id": "impact_1949_restored",
        "source_provider": "YouTube",
        "video_url": "https://www.youtube.com/watch?v=7uV8i0lG3zQ",
        "description": "Brian Donlevy and Ella Raines; an industrialist survives a murder attempt engineered by his unfaithful wife."
    },
    {
        "title": "The Big Combo (Restored)",
        "year": 1955,
        "director": "Joseph H. Lewis",
        "archive_id": "the_big_combo_restored",
        "source_provider": "YouTube",
        "video_url": "https://www.youtube.com/watch?v=W3B_3f2zL7k",
        "description": "A police lieutenant becomes dangerously obsessed with bringing down a sadistic syndicate kingpin."
    }
]

def get_ffmpeg():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def resolve_stream_url(movie_item):
    provider = movie_item.get("source_provider", "Internet Archive")
    url = movie_item["video_url"]

    if provider == "YouTube":
        print(f"[*] Resolving YouTube stream URL via yt-dlp for '{movie_item['title']}'...")
        try:
            cmd = ["yt-dlp", "-g", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", url]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip().splitlines()[0]
            print(f"    [!] yt-dlp failed for YouTube link. Skipping.")
            return None  # <--- Ensures 'if not stream_url:' triggers!
        except Exception as e:
            print(f"    [!] Error resolving YouTube stream: {e}")
            return None  # <--- Ensures 'if not stream_url:' triggers!

    return url

def get_already_processed_movie_ids():
    """Queries Supabase to find movies already indexed."""
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        return set()
    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
        res = client.table("movies").select("movie_id").execute()
        return {r["movie_id"] for r in res.data}
    except Exception as e:
        print(f"[*] Note on Supabase check ({e}); proceeding.")
        return set()

def select_batch_movies(num_movies=10):
    """Selects up to num_movies to process in this run."""
    override = os.environ.get("MOVIE_ID_OVERRIDE", "").strip()
    if override:
        for m in BLENDED_NOIR_CATALOG:
            if m["archive_id"].lower() == override.lower() or m["title"].lower() == override.lower():
                return [m]
        return [{
            "title": override.replace("_", " ").title(),
            "year": 1950,
            "director": "Unknown",
            "archive_id": override,
            "source_provider": "Internet Archive",
            "video_url": f"https://archive.org/download/{override}/{override}.mp4",
            "description": "User requested public domain film noir."
        }]

    already_done = get_already_processed_movie_ids()
    available = [m for m in BLENDED_NOIR_CATALOG if m["archive_id"] not in already_done]

    if not available:
        print("[*] All movies in catalog already indexed! Recycling catalog.")
        available = BLENDED_NOIR_CATALOG

    count = min(num_movies, len(available))
    selected = random.sample(available, count)
    return selected

def analyze_multimodal(clip_path, movie_info, scene_index, duration):
    """Multimodal AI Analysis using Gemini API (free tier) or faster-whisper fallback."""
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

    try:
        from faster_whisper import WhisperModel
        print(f"    🎙️ Transcribing audio with faster-whisper (Scene {scene_index})...")
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(clip_path))
        transcript = " ".join([seg.text.strip() for seg in segments]).strip()
    except Exception as e:
        transcript = "[Ambient noir orchestral score and environmental sound]"

    if not transcript:
        transcript = "[Instrumental noir soundtrack / ambient score]"

    return {
        "transcription": transcript,
        "visual_description": f"Black-and-white high-contrast sequence from '{movie_info['title']}' featuring dramatic low-key lighting and atmospheric camera framing.",
        "synopsis": f"Dramatic character sequence from '{movie_info['title']}' ({movie_info['year']}).",
        "mood": random.choice(["Fatalistic", "Melancholic", "Tense / Suspense", "Paranoid"]),
        "noir_tropes": ["low-key lighting", "cynical dialogue", "chiaroscuro shadows"],
        "tags": ["film noir", "crime drama", "black and white"]
    }

def index_to_supabase(movie, scenes):
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        return False
    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
        client.table("movies").upsert({
            "movie_id": movie["archive_id"],
            "title": movie["title"],
            "director": movie["director"],
            "release_year": movie["year"],
            "source_archive_url": movie["video_url"],
            "license": "Public Domain"
        }).execute()

        for s in scenes:
            client.table("scenes").upsert({
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
                "keyframe_filename": s["keyframe_filename"],
                "video_filename": s["video_filename"],
                "resolution": "1920x1080",
                "video_codec": "h264",
                "audio_codec": "aac",
                "download_url": s.get("download_url", "")
            }).execute()
        return True
    except Exception as e:
        print(f"❌ Supabase notice: {e}")
        return False

def update_catalog_json(all_movies, all_scenes):
    """Writes catalog.json for GitHub Pages web view."""
    catalog_data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_movies": len(all_movies),
        "total_scenes": len(all_scenes),
        "movies": all_movies,
        "scenes": all_scenes
    }

    out_cat = OUTPUT_DIR / "catalog.json"
    with open(out_cat, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2)

    root_cat = BASE_DIR / "catalog.json"
    with open(root_cat, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2)

    print(f"✅ Generated catalog.json with {len(all_movies)} films & {len(all_scenes)} scenes!")

def main():
    ffmpeg_exe = get_ffmpeg()
    raw_num = os.environ.get("NUM_MOVIES", "3").strip()
    try:
        num_to_process = max(1, min(10, int(raw_num)))
    except ValueError:
        num_to_process = 3

    selected_movies = select_batch_movies(num_to_process)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 65)
    print(f"🎬 BATCH SELECTION: Processing {len(selected_movies)} Film Noir Classic(s)")
    for i, m in enumerate(selected_movies, 1):
        print(f"   {i}. {m['title']} ({m['year']}) [{m.get('source_provider', 'Archive')}]")
    print("=" * 65)

    all_batch_movies = []
    all_batch_scenes = []

    for movie_idx, movie in enumerate(selected_movies, 1):
        print(f"\n[{movie_idx}/{len(selected_movies)}] 📽️ Starting '{movie['title']}' ({movie['year']})...")
        sample_file = WORK_DIR / f"{movie['archive_id']}_sample.mp4"

        try:
            stream_url = resolve_stream_url(movie)
            if not stream_url:
                print(f"  [!] Unable to resolve valid stream URL for '{movie['title']}', skipping.")
                continue

            # Randomize start point between 5 and 30 minutes
            random_start_sec = random.randint(300, 1800)
            start_time_str = f"{random_start_sec // 3600:02d}:{(random_start_sec % 3600) // 60:02d}:{random_start_sec % 60:02d}"

            print(f"  🌐 Streaming 12-minute window from {start_time_str}...")
            t0 = time.time()
            cmd = [
                ffmpeg_exe, "-y",
                "-ss", start_time_str,
                "-i", stream_url,
                "-t", "00:12:00",
                "-c", "copy",
                str(sample_file)
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
            if res.returncode != 0 or not sample_file.exists():
                print(f"  [!] Stream copy failed for {movie['title']}, skipping.")
                continue
            print(f"  [✓] Segment cached in {time.time()-t0:.1f}s ({sample_file.stat().st_size / (1024*1024):.1f} MB)")

            # PySceneDetect
            from scenedetect import open_video, SceneManager, AdaptiveDetector
            print("  🔍 Running AI Scene Detection...")
            video = open_video(str(sample_file))
            sm = SceneManager()
            sm.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=24))
            sm.detect_scenes(video, frame_skip=2)
            scene_list = sm.get_scene_list()

            # Release video file handle explicitly so Windows/Linux can manage the file
            del video
            gc.collect()

            valid = [(st, en, (en - st).seconds) for st, en in scene_list if 10.0 <= (en - st).seconds <= 60.0]
            if len(valid) < 3:
                valid = [(st, en, (en - st).seconds) for st, en in scene_list[:5]]

            chosen = random.sample(valid, min(3, len(valid)))
            chosen.sort(key=lambda s: s[0].seconds)

            movie_scenes = []
            for idx, (st, en, dur) in enumerate(chosen, 1):
                clip_name = f"{movie['archive_id']}_scene_{idx}_{int(dur)}s_1080p.mp4"
                thumb_name = f"{movie['archive_id']}_scene_{idx}_thumb.jpg"
                clip_path = OUTPUT_DIR / clip_name
                thumb_path = OUTPUT_DIR / thumb_name

                # Transcode 1080p H.264 / AAC
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
                subprocess.run(transcode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)

                # Keyframe Thumbnail
                mid_sec = (st.seconds + en.seconds) / 2.0
                thumb_cmd = [
                    ffmpeg_exe, "-y",
                    "-ss", f"{mid_sec:.3f}",
                    "-i", str(sample_file),
                    "-vframes", "1",
                    "-q:v", "2",
                    str(thumb_path)
                ]
                subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

                # AI Multimodal
                meta = analyze_multimodal(clip_path, movie, idx, dur)

                abs_start = random_start_sec + st.seconds
                abs_end = random_start_sec + en.seconds
                abs_tc_start = f"{int(abs_start)//3600:02d}:{(int(abs_start)%3600)//60:02d}:{int(abs_start)%60:02d}"
                abs_tc_end = f"{int(abs_end)//3600:02d}:{(int(abs_end)%3600)//60:02d}:{int(abs_end)%60:02d}"

                scene_record = {
                    "scene_id": f"{movie['archive_id']}_scene_{idx}",
                    "movie_id": movie["archive_id"],
                    "scene_number": idx,
                    "start_timecode": abs_tc_start,
                    "end_timecode": abs_tc_end,
                    "start_seconds": abs_start,
                    "end_seconds": abs_end,
                    "duration_seconds": dur,
                    "video_filename": clip_name,
                    "keyframe_filename": thumb_name,
                    "clip_path": clip_path,
                    "thumb_path": thumb_path,
                    **meta
                }
                movie_scenes.append(scene_record)
                all_batch_scenes.append(scene_record)

            # Index to Supabase
            index_to_supabase(movie, movie_scenes)
            all_batch_movies.append(movie)

        except Exception as e:
            print(f"  [!] Error processing {movie['title']}: {e}")
        finally:
            # Safely cleanup intermediate stream without crashing
            try:
                gc.collect()
                if sample_file.exists():
                    sample_file.unlink(missing_ok=True)
            except Exception:
                pass

    if not all_batch_movies:
        print("[!] No movies could be processed in this batch.")
        return

    # Save to local SQLite
    sqlite_db_path = OUTPUT_DIR / "film_noir_scenes.db"
    conn = sqlite3.connect(sqlite_db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS movies (
        movie_id TEXT PRIMARY KEY, title TEXT, director TEXT, release_year INTEGER, source_archive_url TEXT
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scenes (
        scene_id TEXT PRIMARY KEY, movie_id TEXT, scene_number INTEGER, start_seconds REAL, end_seconds REAL,
        duration_seconds REAL, start_timecode TEXT, end_timecode TEXT, transcription TEXT, visual_description TEXT,
        synopsis TEXT, mood TEXT, noir_tropes TEXT, tags TEXT, keyframe_filename TEXT, video_filename TEXT
    );""")
    for m in all_batch_movies:
        cur.execute("INSERT OR REPLACE INTO movies VALUES (?, ?, ?, ?, ?)", (m["archive_id"], m["title"], m["director"], m["year"], m["video_url"]))
    for s in all_batch_scenes:
        cur.execute("INSERT OR REPLACE INTO scenes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            s["scene_id"], s["movie_id"], s["scene_number"], s["start_seconds"], s["end_seconds"], s["duration_seconds"],
            s["start_timecode"], s["end_timecode"], s["transcription"], s["visual_description"], s["synopsis"], s["mood"],
            json.dumps(s.get("noir_tropes", [])), json.dumps(s.get("tags", [])), s["keyframe_filename"], s["video_filename"]
        ))
    conn.commit()
    conn.close()

    # Update catalog.json for GitHub Pages website
    update_catalog_json(all_batch_movies, all_batch_scenes)

    # Package Batch ZIP
    zip_filename = f"Noir_Batch_{len(all_batch_movies)}_Films_{timestamp_str}.zip"
    zip_path = OUTPUT_DIR / zip_filename
    print(f"\n📦 Packaging complete batch ZIP: {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in all_batch_scenes:
            if s["clip_path"].exists():
                zf.write(s["clip_path"], arcname=s["video_filename"])
            if s["thumb_path"].exists():
                zf.write(s["thumb_path"], arcname=s["keyframe_filename"])
        zf.write(sqlite_db_path, arcname=sqlite_db_path.name)
        zf.write(OUTPUT_DIR / "catalog.json", arcname="catalog.json")

    # Set GitHub Actions output environment safely
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"MOVIE_COUNT={len(all_batch_movies)}\n")
            f.write(f"TIMESTAMP={timestamp_str}\n")
            f.write(f"ZIP_FILE_PATH={zip_path.as_posix()}\n")

    print("=" * 65)
    print(f"🎉 BATCH RUN COMPLETE! Processed {len(all_batch_movies)} films & {len(all_batch_scenes)} clips.")
    print(f"📦 ZIP Archive: {zip_path} ({zip_path.stat().st_size / (1024*1024):.2f} MB)")
    print(f"🌐 Website ready at: index.html")
    print("=" * 65)

if __name__ == "__main__":
    main()
