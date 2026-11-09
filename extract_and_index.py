"""
Multi-Source Film & Genre Scene Extractor & Cloud Indexer
Dynamically queries Internet Archive based on workflow parameters, extracts scenes, 
generates AI metadata, and updates Supabase & GitHub Pages.
"""

import os
import sys
import json
import time
import random
import shutil
import sqlite3
import subprocess
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

# --- Dynamic Configuration from Workflow Inputs ---
SOURCE_PROVIDER = os.environ.get("SOURCE_PROVIDER", "Internet Archive").strip()
ARCHIVE_CATEGORY = os.environ.get("ARCHIVE_CATEGORY", "feature_films").strip()
GENRE_KEYWORD = os.environ.get("GENRE_KEYWORD", "film noir").strip().lower()
NUM_MOVIES = int(os.environ.get("NUM_MOVIES", "3"))
CLIPS_PER_MOVIE = int(os.environ.get("CLIPS_PER_MOVIE", "3"))
MIN_CLIP_LEN = float(os.environ.get("MIN_CLIP_LEN", "8.0"))
MAX_CLIP_LEN = float(os.environ.get("MAX_CLIP_LEN", "60.0"))
START_YEAR = int(os.environ.get("START_YEAR", "1930"))
END_YEAR = int(os.environ.get("END_YEAR", "1960"))
LICENSE_TYPE = os.environ.get("LICENSE_TYPE", "any").strip().lower()

def get_ffmpeg():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

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

def fetch_random_archive_candidates(candidate_count=20):
    """Queries the Internet Archive API dynamically based on workflow form parameters."""
    already_done = get_already_processed_movie_ids()
    search_url = "https://archive.org/advancedsearch.php"
    query_parts = ['mediatype:movies', 'format:"MPEG4"']
    
    if ARCHIVE_CATEGORY != "all_movies":
        query_parts.append(f'collection:"{ARCHIVE_CATEGORY}"')
        
    if GENRE_KEYWORD != "any":
        query_parts.append(f'subject:"{GENRE_KEYWORD}"')
        
    if LICENSE_TYPE != "any":
        query_parts.append(f'licenseurl:*"{LICENSE_TYPE.replace(" ", "")}"* OR rights:"*{LICENSE_TYPE}*"')
        
    query = " AND ".join(query_parts)
    random_page = random.randint(1, 5)
    
    params = {
        "q": query,
        "fl[]": "identifier,title,year,creator,description",
        "sort[]": "downloads desc",
        "rows": 50,
        "page": random_page,
        "output": "json"
    }
    
    try:
        print(f"🔎 Searching Archive API ({SOURCE_PROVIDER} / {ARCHIVE_CATEGORY}) | Genre: '{GENRE_KEYWORD}' | License: '{LICENSE_TYPE}' ({START_YEAR}-{END_YEAR})...")
        resp = requests.get(search_url, params=params, timeout=15)
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            print(f"    [✓] Retrieved {len(docs)} candidate items from Archive API.")
            random.shuffle(docs)
            candidates = []
            for doc in docs:
                archive_id = doc.get("identifier")
                if not archive_id or archive_id in already_done:
                    continue
                
                rel_year = int(doc.get("year", 1950)) if str(doc.get("year", "")).isdigit() else 1950
                if not (START_YEAR <= rel_year <= END_YEAR):
                    continue
                
                meta_url = f"https://archive.org/metadata/{archive_id}"
                try:
                    m_resp = requests.get(meta_url, timeout=10)
                    if m_resp.status_code == 200:
                        files = m_resp.json().get("files", [])
                        mp4_files = [f for f in files if f.get("name", "").endswith(".mp4") and not f.get("name", "").endswith("_512kb.mp4")]
                        if not mp4_files:
                            mp4_files = [f for f in files if f.get("name", "").endswith(".mp4")]
                        if mp4_files:
                            mp4_files.sort(key=lambda x: int(x.get("size", 0)), reverse=True)
                            best_mp4 = mp4_files[0]["name"]
                            candidates.append({
                                "title": doc.get("title", archive_id.replace("_", " ").title()),
                                "year": rel_year,
                                "director": doc.get("creator", "Unknown Director"),
                                "archive_id": archive_id,
                                "source_provider": SOURCE_PROVIDER,
                                "video_url": f"https://archive.org/download/{archive_id}/{best_mp4}",
                                "license": LICENSE_TYPE.title() if LICENSE_TYPE != "any" else "Open Archive",
                                "description": doc.get("description", f"Archival video feature.")
                            })
                except Exception:
                    continue
                if len(candidates) >= candidate_count:
                    break
            return candidates
    except Exception as e:
        print(f"  [!] Dynamic catalog fetch error: {e}")
    return []

def analyze_multimodal(clip_path, movie_info, scene_index, duration):
    """Multi-Tier AI Metadata Generator (Gemini -> Hugging Face -> Faster-Whisper)."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            print(f"    🤖 [1/3] Calling Gemini API for Scene {scene_index}...")
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=gemini_key)
            uploaded_file = client.files.upload(file=str(clip_path))
            prompt = f"""
            Analyze this scene clip from '{movie_info['title']}' ({movie_info['year']}) and respond in strict JSON format:
            {{"transcription": "Verbatim spoken dialogue or [Instrumental/Ambient]", "visual_description": "Detailed staging, lighting, and camera framing", "synopsis": "Concise 2-3 sentence narrative summary of the dramatic beat", "mood": "Dominant emotional tone", "noir_tropes": ["list", "of", "tropes", "detected"], "tags": ["relevant", "keywords"]}}
            """
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"    [!] Gemini notice ({e}), attempting Hugging Face fallback...")

    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if hf_token:
        try:
            print(f"    🤗 [2/3] Calling Hugging Face Free AI Inference for Scene {scene_index}...")
            headers = {"Authorization": f"Bearer {hf_token}"}
            hf_url = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2"
            prompt = f"<s>[INST] Generate JSON analysis metadata for a scene in the film '{movie_info['title']}' ({movie_info['year']}). Respond ONLY in valid JSON with keys: transcription, visual_description, synopsis, mood, noir_tropes, tags. [/INST]"
            resp = requests.post(hf_url, headers=headers, json={"inputs": prompt, "parameters": {"max_new_tokens": 350}}, timeout=15)
            if resp.status_code == 200:
                raw_text = resp.json()[0]["generated_text"]
                clean_json = raw_text.split("[/INST]")[-1].strip()
                return json.loads(clean_json)
        except Exception as hf_err:
            print(f"    [!] Hugging Face notice ({hf_err}), falling back to local whisper...")

    try:
        from faster_whisper import WhisperModel
        print(f"    🎙️ [3/3] Transcribing audio with local faster-whisper (Scene {scene_index})...")
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(clip_path))
        transcript = " ".join([seg.text.strip() for seg in segments]).strip()
    except Exception as e:
        transcript = "[Ambient orchestral score and environmental sound]"

    if not transcript:
        transcript = "[Instrumental soundtrack / ambient score]"

    return {
        "transcription": transcript,
        "visual_description": f"Sequence from '{movie_info['title']}' featuring dramatic lighting and atmospheric framing.",
        "synopsis": f"Dramatic character sequence from '{movie_info['title']}' ({movie_info['year']}).",
        "mood": random.choice(["Fatalistic", "Melancholic", "Tense / Suspense", "Paranoid"]),
        "noir_tropes": ["low-key lighting", "cynical dialogue", "dramatic framing"],
        "tags": [GENRE_KEYWORD if GENRE_KEYWORD != "any" else "classic cinema", "black and white"]
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
            "license": movie.get("license", "Unspecified")
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
    """Merges new batch records with existing historical scenes so catalog.json grows over time."""
    root_cat = BASE_DIR / "catalog.json"
    existing_movies = []
    existing_scenes = []

    # Load existing history if catalog.json exists
    if root_cat.exists():
        try:
            with open(root_cat, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                existing_movies = old_data.get("movies", [])
                existing_scenes = old_data.get("scenes", [])
        except Exception as e:
            print(f"[*] Notice loading existing catalog.json ({e})")

    # Combine & deduplicate movies by archive_id
    movie_dict = {m["archive_id"]: m for m in existing_movies}
    for m in all_movies:
        movie_dict[m["archive_id"]] = m

    # Combine & deduplicate scenes by scene_id
    scene_dict = {s["scene_id"]: s for s in existing_scenes}
    for s in all_scenes:
        s_copy = dict(s)
        if "clip_path" in s_copy:
            s_copy["clip_path"] = str(s_copy["clip_path"])
        if "thumb_path" in s_copy:
            s_copy["thumb_path"] = str(s_copy["thumb_path"])
        scene_dict[s["scene_id"]] = s_copy

    combined_movies = list(movie_dict.values())
    combined_scenes = list(scene_dict.values())

    catalog_data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_movies": len(combined_movies),
        "total_scenes": len(combined_scenes),
        "genre": GENRE_KEYWORD,
        "source": SOURCE_PROVIDER,
        "category": ARCHIVE_CATEGORY,
        "movies": combined_movies,
        "scenes": combined_scenes
    }

    out_cat = OUTPUT_DIR / "catalog.json"
    with open(out_cat, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2)

    with open(root_cat, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2)
        
    print(f"✅ Catalog updated! Total archive size: {len(combined_movies)} movies & {len(combined_scenes)} scenes.")

def main():
    ffmpeg_exe = get_ffmpeg()
    movie_pool = fetch_random_archive_candidates(candidate_count=20)
    
    print("=" * 65)
    print(f"🎬 TARGET GOAL: Successfully Extract Scenes for {NUM_MOVIES} Film(s)")
    print(f"📋 Candidate Pool: {len(movie_pool)} films | Source: {SOURCE_PROVIDER} ({ARCHIVE_CATEGORY}) | Genre: '{GENRE_KEYWORD}'")
    print("=" * 65)

    all_batch_movies = []
    all_batch_scenes = []
    pool_index = 0

    while len(all_batch_movies) < NUM_MOVIES and pool_index < len(movie_pool):
        movie = movie_pool[pool_index]
        pool_index += 1
        print(f"\n[{len(all_batch_movies) + 1}/{NUM_MOVIES}] 📽️ Attempting '{movie['title']}' ({movie['year']})...")
        sample_file = WORK_DIR / f"{movie['archive_id']}_sample.mp4"

        try:
            stream_url = movie["video_url"]
            random_start_sec = random.randint(300, 1800)
            start_time_str = f"{random_start_sec // 3600:02d}:{(random_start_sec % 3600) // 60:02d}:{random_start_sec % 60:02d}"
            
            print(f"  🌐 Streaming 15-minute window from {start_time_str}...")
            t0 = time.time()
            cmd = [
                ffmpeg_exe, "-y", "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
                "-ss", start_time_str, "-i", stream_url, "-t", "00:15:00",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-c:a", "aac", str(sample_file)
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)

            if res.returncode != 0 or not sample_file.exists() or sample_file.stat().st_size < 100000:
                print(f"  [!] FFmpeg stream slice dropped. Running Python HTTP chunk fallback...")
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    with requests.get(stream_url, headers=headers, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        downloaded = 0
                        with open(sample_file, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024*1024):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                if downloaded >= 80 * 1024 * 1024:
                                    break
                except Exception as dl_err:
                    print(f"  [!] Fallback download notice: {dl_err}")

            if not sample_file.exists() or sample_file.stat().st_size < 100000:
                print(f"  [!] Download failed for '{movie['title']}'. Moving on...")
                continue

            print(f"  [✓] Segment cached in {time.time()-t0:.1f}s ({sample_file.stat().st_size / (1024*1024):.1f} MB)")

            from scenedetect import open_video, SceneManager, AdaptiveDetector
            print("  🔍 Running AI Scene Detection...")
            video = open_video(str(sample_file))
            sm = SceneManager()
            sm.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=24))
            sm.detect_scenes(video, frame_skip=2)
            scene_list = sm.get_scene_list()
            del video
            gc.collect()

            valid = [(st, en, (en - st).seconds) for st, en in scene_list if MIN_CLIP_LEN <= (en - st).seconds <= MAX_CLIP_LEN]
            if len(valid) < CLIPS_PER_MOVIE:
                valid = [(st, en, (en - st).seconds) for st, en in scene_list[:CLIPS_PER_MOVIE]]

            if not valid:
                print(f"  [!] No valid scenes detected for '{movie['title']}'. Moving on...")
                continue

            chosen = random.sample(valid, min(CLIPS_PER_MOVIE, len(valid)))
            chosen.sort(key=lambda s: s[0].seconds)

            movie_scenes = []
            for idx, (st, en, dur) in enumerate(chosen, 1):
                clip_name = f"{movie['archive_id']}_scene_{idx}_{int(dur)}s_1080p.mp4"
                thumb_name = f"{movie['archive_id']}_scene_{idx}_thumb.jpg"
                clip_path = OUTPUT_DIR / clip_name
                thumb_path = OUTPUT_DIR / thumb_name

                transcode_cmd = [
                    ffmpeg_exe, "-y", "-ss", st.get_timecode(), "-to", en.get_timecode(), "-i", str(sample_file),
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "19",
                    "-c:a", "aac", "-b:a", "192k", "-fflags", "+genpts", "-movflags", "+faststart",
                    str(clip_path)
                ]
                subprocess.run(transcode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)

                mid_sec = (st.seconds + en.seconds) / 2.0
                thumb_cmd = [
                    ffmpeg_exe, "-y", "-ss", f"{mid_sec:.3f}", "-i", str(sample_file),
                    "-vframes", "1", "-q:v", "2", str(thumb_path)
                ]
                subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

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

            index_to_supabase(movie, movie_scenes)
            all_batch_movies.append(movie)
            print(f"  [✓] Successfully processed '{movie['title']}' ({len(all_batch_movies)}/{NUM_MOVIES})")

        except Exception as e:
            print(f"  [!] Exception processing {movie['title']}: {e}")
        finally:
            try:
                gc.collect()
                if sample_file.exists():
                    sample_file.unlink(missing_ok=True)
            except Exception:
                pass

    if not all_batch_movies:
        print("[!] Could not process any movies from dynamic pool.")
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
            scene_id TEXT PRIMARY KEY, movie_id TEXT, scene_number INTEGER, start_seconds REAL, end_seconds REAL, duration_seconds REAL, start_timecode TEXT, end_timecode TEXT, transcription TEXT, visual_description TEXT, synopsis TEXT, mood TEXT, noir_tropes TEXT, tags TEXT, keyframe_filename TEXT, video_filename TEXT
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

    print("=" * 65)
    print(f"🎉 BATCH RUN COMPLETE! Processed {len(all_batch_movies)} films & {len(all_batch_scenes)} clips.")
    print(f"🌐 Website ready at: index.html")
    print("=" * 65)

if __name__ == "__main__":
    main()
