#!/usr/bin/env python3
"""
Standalone Clip Analyzer, Uploader & Multi-Model Re-Analyzer
============================================================
Optional add-on script that:
1. Ingests pre-cut local clips from a folder (e.g. ingest_clips/), analyzes them,
   uploads to R2, and indexes into Supabase.
2. Re-analyzes existing R2/Supabase clips using selectable AI models
   (gemini-2.5-flash, gemini-2.5-pro, hf-qwen, or local-clip-whisper) and stores
   every run in public.scene_analyses for side-by-side model comparison.
3. Respects free-tier API quotas by pacing requests and stopping gracefully on 429.
"""
import os
import re
import json
import time
import shutil
import subprocess
from pathlib import Path
import requests
import imageio_ffmpeg

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "temp_reanalyze"
WORK_DIR.mkdir(parents=True, exist_ok=True)

RUN_MODE = os.environ.get("RUN_MODE", "reanalyze_r2").strip()
INGEST_FOLDER = os.environ.get("INGEST_FOLDER", "ingest_clips").strip()
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash").strip()
PROMPT_STYLE = os.environ.get("PROMPT_STYLE", "cinematic_noir").strip()
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "20"))
REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "4.5"))
UPDATE_MAIN_SCENE = os.environ.get("UPDATE_MAIN_SCENE", "true").strip().lower() == "true"
FORCE_REANALYZE = os.environ.get("FORCE_REANALYZE", "false").strip().lower() == "true"
FILTER_MOOD = os.environ.get("FILTER_MOOD", "all").strip()

_WHISPER_MODEL = None


class FreeQuotaExhaustedError(Exception):
    pass


def get_ffmpeg():
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def get_whisper():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def transcribe_audio(video_path: Path) -> str:
    try:
        model = get_whisper()
        segments, _ = model.transcribe(str(video_path), vad_filter=True)
        text = " ".join([s.text.strip() for s in segments if s.text.strip()]).strip()
        return text if text else "[Instrumental soundtrack / ambient score]"
    except Exception as e:
        print(f"    [!] Whisper notice ({e})")
        return "[Ambient score]"


def extract_keyframes(video_path: Path, prefix: str, count: int = 3) -> list[Path]:
    ffmpeg = get_ffmpeg()
    res = subprocess.run([ffmpeg, "-i", str(video_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    duration = 15.0
    if dur_match:
        h, m, s = float(dur_match.group(1)), float(dur_match.group(2)), float(dur_match.group(3))
        duration = max(2.0, h * 3600 + m * 60 + s)

    frames = []
    for idx, frac in enumerate([0.2, 0.5, 0.8][:count], 1):
        ts = max(0.2, duration * frac)
        out_jpg = WORK_DIR / f"{prefix}_kf{idx}.jpg"
        cmd = [
            ffmpeg, "-y", "-ss", f"{ts:.2f}", "-i", str(video_path),
            "-vframes", "1", "-vf", "scale=768:-2", "-q:v", "3", str(out_jpg)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if out_jpg.exists():
            frames.append(out_jpg)
    return frames


def build_prompt(title: str, year: int, transcript: str, existing_visual: str = "") -> str:
    style_instructions = {
        "cinematic_noir": "Focus on classic cinema atmosphere, chiaroscuro lighting, shadow interplay, character tension, and narrative subtext.",
        "technical_dp": "Focus on cinematography craft: shot scale (CU/MS/WS), camera angle, contrast ratio, key/fill lighting direction, lens depth, and blocking.",
        "archival_concise": "Provide crisp, factual archival cataloging suitable for fast editorial search.",
    }
    directive = style_instructions.get(PROMPT_STYLE, style_instructions["cinematic_noir"])
    return f"""
Analyze this film scene from '{title}' ({year}).
Style Directive: {directive}
Extracted Audio Dialogue: "{transcript}"
Prior Visual Hint: "{existing_visual}"

Inspect the attached scene keyframes and dialogue, then return ONLY a valid JSON object with exact keys:
{{
  "transcription": "cleaned dialogue or audio description",
  "visual_description": "2 vivid sentences describing lighting, framing, staging, and camera composition",
  "synopsis": "2-3 sentence narrative beat breakdown",
  "mood": "one of: Fatalistic, Melancholic, Tense / Suspense, Paranoid",
  "noir_tropes": ["3 to 5 specific visual or narrative tropes"],
  "tags": ["5 to 8 searchable keywords"]
}}
""".strip()


def extract_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    return json.loads(m.group(0) if m else cleaned)


def analyze_with_selected_model(video_path: Path, keyframes: list[Path], title: str, year: int, existing_transcript: str = "", existing_visual: str = "") -> tuple[str, dict]:
    transcript = existing_transcript.strip() if existing_transcript and not existing_transcript.startswith("Archival classic") else ""
    if not transcript:
        transcript = transcribe_audio(video_path)

    prompt = build_prompt(title, year, transcript, existing_visual)

    if AI_MODEL.startswith("gemini-"):
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY secret is not configured.")

        from google import genai
        from google.genai import types
        from PIL import Image

        client = genai.Client(api_key=gemini_key)
        contents = [Image.open(kf) for kf in keyframes if kf.exists()]
        contents.append(prompt)

        try:
            response = client.models.generate_content(
                model=AI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=TEMPERATURE,
                ),
            )
            parsed = extract_json(response.text)
            parsed.setdefault("transcription", transcript)
            return "google-gemini", parsed
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                raise FreeQuotaExhaustedError(f"Gemini free-tier quota reached: {err_str}")
            raise

    if AI_MODEL == "hf-qwen":
        hf_token = os.environ.get("HF_TOKEN", "").strip()
        if not hf_token:
            raise RuntimeError("HF_TOKEN secret is not configured.")
        resp = requests.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {hf_token}", "Content-Type": "application/json"},
            json={
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": TEMPERATURE,
            },
            timeout=25,
        )
        if resp.status_code == 429:
            raise FreeQuotaExhaustedError("Hugging Face free-tier rate limit reached.")
        resp.raise_for_status()
        parsed = extract_json(resp.json()["choices"][0]["message"]["content"])
        parsed.setdefault("transcription", transcript)
        return "huggingface-router", parsed

    from extract_and_index import analyze_keyframe_with_clip
    vis = analyze_keyframe_with_clip(keyframes[len(keyframes) // 2] if keyframes else None)
    return "local-cpu", {
        "transcription": transcript,
        "visual_description": f"Features {vis['shot_style']}, accented by {', '.join(vis['noir_tropes'][:2])}.",
        "synopsis": f"Sequence from '{title}' ({year}) marked by a {vis['mood'].lower()} atmosphere.",
        "mood": vis["mood"],
        "noir_tropes": vis["noir_tropes"],
        "tags": vis["tags"],
    }


def upload_to_r2(local_path: Path, r2_key: str, content_type: str) -> str:
    import boto3
    from botocore.config import Config

    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket_name = os.environ.get("R2_BUCKET_NAME", "").strip()
    public_url = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")

    if not all([account_id, access_key, secret_key, bucket_name, public_url]):
        return ""

    s3 = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    s3.upload_file(str(local_path), bucket_name, r2_key, ExtraArgs={"ContentType": content_type})
    return f"{public_url}/{r2_key}"


def run_reanalyze_r2(sb):
    print(f"Re-analyzing R2/Supabase clips | Model: {AI_MODEL} | Style: {PROMPT_STYLE} | Temp: {TEMPERATURE}")

    done_ids = set()
    if not FORCE_REANALYZE:
        existing = sb.table("scene_analyses").select("scene_id").eq("ai_model", AI_MODEL).eq("prompt_style", PROMPT_STYLE).execute()
        done_ids = {r["scene_id"] for r in (existing.data or [])}

    query = sb.table("scenes").select("*, movies(title, release_year, director)").order("created_at", desc=True)
    if FILTER_MOOD != "all":
        query = query.ilike("mood", f"%{FILTER_MOOD}%")

    scenes = (query.limit(500).execute()).data or []
    candidates = [s for s in scenes if s.get("download_url") and s["scene_id"] not in done_ids]
    print(f"  [OK] {len(candidates)} candidate scene(s) eligible (skipping {len(done_ids)} already analyzed).")

    processed = 0
    for sc in candidates[:BATCH_LIMIT]:
        scene_id = sc["scene_id"]
        movie_meta = sc.get("movies") or {}
        title = movie_meta.get("title") or sc.get("movie_id", "Classic Film")
        year = movie_meta.get("release_year") or 1950
        dl_url = sc["download_url"]

        local_clip = WORK_DIR / f"{scene_id}.mp4"
        keyframes = []
        try:
            print(f"[{processed + 1}/{min(BATCH_LIMIT, len(candidates))}] Analyzing {scene_id} ({title})...")
            with requests.get(dl_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_clip, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

            keyframes = extract_keyframes(local_clip, scene_id, count=3)
            provider, result = analyze_with_selected_model(
                local_clip, keyframes, title, year,
                existing_transcript=sc.get("transcription", ""),
                existing_visual=sc.get("visual_description", ""),
            )

            analysis_id = f"{scene_id}__{AI_MODEL}__{PROMPT_STYLE}"
            sb.table("scene_analyses").upsert({
                "analysis_id": analysis_id,
                "scene_id": scene_id,
                "ai_provider": provider,
                "ai_model": AI_MODEL,
                "prompt_style": PROMPT_STYLE,
                "temperature": TEMPERATURE,
                "transcription": result.get("transcription", ""),
                "visual_description": result.get("visual_description", ""),
                "synopsis": result.get("synopsis", ""),
                "mood": result.get("mood", sc.get("mood", "Fatalistic")),
                "noir_tropes": result.get("noir_tropes", []),
                "tags": result.get("tags", []),
                "raw_response": result,
            }).execute()

            if UPDATE_MAIN_SCENE:
                sb.table("scenes").update({
                    "transcription": result.get("transcription", sc.get("transcription")),
                    "visual_description": result.get("visual_description", sc.get("visual_description")),
                    "synopsis": result.get("synopsis", sc.get("synopsis")),
                    "mood": result.get("mood", sc.get("mood")),
                    "noir_tropes": result.get("noir_tropes", sc.get("noir_tropes", [])),
                    "tags": result.get("tags", sc.get("tags", [])),
                }).eq("scene_id", scene_id).execute()

            processed += 1
            print(f"  [OK] Saved analysis '{analysis_id}' (Mood: {result.get('mood')})")
            time.sleep(REQUEST_DELAY_SEC)

        except FreeQuotaExhaustedError as q_err:
            print(f"Quota reached: {q_err}. Stopping cleanly after {processed} scene(s).")
            break
        except Exception as e:
            print(f"  [!] Error analyzing {scene_id}: {e}")
        finally:
            local_clip.unlink(missing_ok=True)
            for kf in keyframes:
                kf.unlink(missing_ok=True)

    print(f"Re-analysis complete! Processed {processed} scene(s) with {AI_MODEL}.")


def run_ingest_local(sb):
    folder = BASE_DIR / INGEST_FOLDER
    if not folder.exists():
        print(f"Folder '{folder}' does not exist. Create it and add .mp4 clips to ingest.")
        return

    clips = sorted(folder.glob("*.mp4"))
    print(f"Found {len(clips)} local MP4 clip(s) in '{INGEST_FOLDER}/'...")

    processed = 0
    for clip in clips[:BATCH_LIMIT]:
        stem = clip.stem
        movie_id = stem.split("_scene_")[0] if "_scene_" in stem else re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:30]
        scene_id = stem
        keyframes = []
        try:
            print(f"[{processed + 1}/{min(BATCH_LIMIT, len(clips))}] Ingesting {clip.name}...")
            keyframes = extract_keyframes(clip, stem, count=3)
            mid_thumb = keyframes[len(keyframes) // 2] if keyframes else None

            video_url = upload_to_r2(clip, f"clips/{clip.name}", "video/mp4")
            thumb_name = f"{stem}_thumb.jpg"
            thumb_url = upload_to_r2(mid_thumb, f"thumbs/{thumb_name}", "image/jpeg") if mid_thumb else ""

            title = movie_id.replace("_", " ").replace("-", " ").title()
            provider, result = analyze_with_selected_model(clip, keyframes, title, 1950)

            sb.table("movies").upsert({
                "movie_id": movie_id,
                "title": title,
                "director": "Custom Ingest",
                "release_year": 1950,
                "source_archive_url": video_url,
                "license": "Custom / Archival",
            }).execute()

            sb.table("scenes").upsert({
                "scene_id": scene_id,
                "movie_id": movie_id,
                "scene_number": processed + 1,
                "start_seconds": 0.0,
                "end_seconds": 15.0,
                "duration_seconds": 15.0,
                "start_timecode": "00:00:00",
                "end_timecode": "00:00:15",
                "transcription": result.get("transcription", ""),
                "visual_description": result.get("visual_description", ""),
                "synopsis": result.get("synopsis", ""),
                "mood": result.get("mood", "Fatalistic"),
                "noir_tropes": result.get("noir_tropes", []),
                "tags": result.get("tags", []),
                "keyframe_filename": thumb_name,
                "video_filename": clip.name,
                "download_url": video_url,
                "thumbnail_url": thumb_url,
            }).execute()

            sb.table("scene_analyses").upsert({
                "analysis_id": f"{scene_id}__{AI_MODEL}__{PROMPT_STYLE}",
                "scene_id": scene_id,
                "ai_provider": provider,
                "ai_model": AI_MODEL,
                "prompt_style": PROMPT_STYLE,
                "temperature": TEMPERATURE,
                "transcription": result.get("transcription", ""),
                "visual_description": result.get("visual_description", ""),
                "synopsis": result.get("synopsis", ""),
                "mood": result.get("mood", "Fatalistic"),
                "noir_tropes": result.get("noir_tropes", []),
                "tags": result.get("tags", []),
                "raw_response": result,
            }).execute()

            processed += 1
            print(f"  [OK] Uploaded & indexed {clip.name} -> {video_url}")
            time.sleep(REQUEST_DELAY_SEC)

        except FreeQuotaExhaustedError as q_err:
            print(f"Quota reached: {q_err}")
            break
        except Exception as e:
            print(f"  [!] Error ingesting {clip.name}: {e}")
        finally:
            for kf in keyframes:
                kf.unlink(missing_ok=True)

    print(f"Local ingest complete! Processed {processed} clip(s).")


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY.")

    from supabase import create_client
    sb = create_client(supabase_url, supabase_key)

    if RUN_MODE == "ingest_local":
        run_ingest_local(sb)
    else:
        run_reanalyze_r2(sb)


if __name__ == "__main__":
    main()
