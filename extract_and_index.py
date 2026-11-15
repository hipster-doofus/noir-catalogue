"""
Multi-Source Film & Genre Scene Extractor & Cloud Indexer
- Fast single-pass FFmpeg stream-copy slicing (-c copy) from Internet Archive
- Local CPU multimodal perception: faster-whisper (dialogue) + CLIP (mood, tropes, 512-d vector embeddings)
- Free-tier text-only LLM enrichment (Gemini 2.5 Flash / Hugging Face Router)
- Zero-bloat media offloading to Cloudflare R2 or GitHub Releases (vault-media)
- Syncs Supabase (with pgvector), local SQLite, and catalog.json
"""
import os
import re
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

# Seed entropy
random.seed(int(time.time() * 1000) ^ os.getpid())

# Setup workspace directories
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = BASE_DIR / "temp_workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Dynamic Environment Variables
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

# Lazy-loaded local AI singletons
_WHISPER_MODEL = None
_CLIP_MODEL = None
_CLIP_PROCESSOR = None

NOIR_MOOD_PROMPTS = {
    "Fatalistic": "a dark fatalistic film noir scene of doomed characters in heavy shadows",
    "Melancholic": "a lonely melancholic black and white movie scene with wistful reflection",
    "Tense / Suspense": "a tense suspenseful thriller scene with danger and confrontation",
    "Paranoid": "a paranoid shadowy noir scene of a detective looking over their shoulder"
}

NOIR_TROPE_PROMPTS = {
    "low-key chiaroscuro": "high contrast chiaroscuro black and white lighting with deep shadows",
    "venetian blind shadows": "dramatic venetian blind shadow stripes cast across a dark room",
    "cigarette smoke": "character smoking a cigarette in dramatic atmospheric haze",
    "rain-slicked streets": "wet rain-slicked city street at night reflecting streetlights",
    "fedora & trenchcoat": "detective wearing a fedora hat and trenchcoat in shadow",
    "femme fatale": "glamorous mysterious femme fatale woman in classic 1940s cinema",
    "interrogation / standoff": "tense face-to-face confrontation or interrogation in a dim room",
    "dutch angle framing": "tilted off-balance camera angle creating psychological tension",
    "nightclub / jazz bar": "smoky vintage nightclub or bar scene with drinks and shadows",
    "telephone / late-night call": "character making an urgent late-night telephone call in the dark"
}

SHOT_STYLE_PROMPTS = [
    "an intimate close-up shot focusing on facial expression and shadow",
    "a dramatic medium two-shot of characters in tense conversation",
    "a wide atmospheric establishing shot showing deep architectural shadows",
    "a stark silhouette framed against a bright doorway or window"
]


def get_ffmpeg():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        print("  🎙️ Loading local faster-whisper (base.en, int8)...")
        _WHISPER_MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def get_clip_components():
    global _CLIP_MODEL, _CLIP_PROCESSOR
    if _CLIP_MODEL is None or _CLIP_PROCESSOR is None:
        from transformers import CLIPProcessor, CLIPModel
        model_id = "openai/clip-vit-base-patch32"
        print(f"  👁️ Loading local CLIP vision-language model ({model_id})...")
        _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(model_id)
        _CLIP_MODEL = CLIPModel.from_pretrained(model_id)
        _CLIP_MODEL.eval()
    return _CLIP_MODEL, _CLIP_PROCESSOR


def transcribe_clip_audio(clip_path):
    """Extracts spoken dialogue using local faster-whisper."""
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(str(clip_path), vad_filter=True)
        transcript = " ".join([seg.text.strip() for seg in segments if seg.text.strip()]).strip()
        return transcript if transcript else "[Instrumental soundtrack / ambient score]"
    except Exception as e:
        print(f"    [!] Whisper transcription notice ({e})")
        return "[Ambient score and environmental dialogue]"


def analyze_keyframe_with_clip(thumb_path):
    """Runs zero-shot CLIP classification + extracts normalized 512-d image embedding."""
    default_result = {
        "mood": "Tense / Suspense",
        "noir_tropes": ["low-key chiaroscuro", "dramatic framing"],
        "shot_style": "a dramatic medium shot with classic noir lighting",
        "tags": ["classic cinema", "black and white"],
        "embedding": None
    }
    if not thumb_path or not Path(thumb_path).exists():
        return default_result

    try:
        import torch
        from PIL import Image

        model, processor = get_clip_components()
        image = Image.open(thumb_path).convert("RGB")

        mood_labels = list(NOIR_MOOD_PROMPTS.keys())
        mood_texts = list(NOIR_MOOD_PROMPTS.values())

        trope_labels = list(NOIR_TROPE_PROMPTS.keys())
        trope_texts = list(NOIR_TROPE_PROMPTS.values())

        all_texts = mood_texts + trope_texts + SHOT_STYLE_PROMPTS
        inputs = processor(text=all_texts, images=image, return_tensors="pt", padding=True)

        with torch.no_grad():
            outputs = model(**inputs)
            logits = outputs.logits_per_image[0]
            img_embeds = outputs.image_embeds[0]
            norm_embed = (img_embeds / img_embeds.norm(p=2)).cpu().tolist()

            m_end = len(mood_texts)
            t_end = m_end + len(trope_texts)

            mood_probs = logits[:m_end].softmax(dim=-1)
            trope_probs = logits[m_end:t_end].softmax(dim=-1)
            shot_probs = logits[t_end:].softmax(dim=-1)

            best_mood = mood_labels[int(mood_probs.argmax().item())]
            top_trope_indices = torch.topk(trope_probs, k=3).indices.tolist()
            detected_tropes = [trope_labels[i] for i in top_trope_indices]
            best_shot = SHOT_STYLE_PROMPTS[int(shot_probs.argmax().item())]

        tags = list(dict.fromkeys(
            ([GENRE_KEYWORD] if GENRE_KEYWORD != "any" else ["classic cinema"])
            + detected_tropes
            + [best_mood.lower().split("/")[0].strip()]
        ))

        return {
            "mood": best_mood,
            "noir_tropes": detected_tropes,
            "shot_style": best_shot,
            "tags": tags,
            "embedding": norm_embed
        }
    except Exception as e:
        print(f"    [!] CLIP visual perception notice ({e})")
        return default_result


def offload_asset_to_cloud(local_path: Path):
    """
    Uploads MP4 or JPG to Cloudflare R2 (if R2_* env vars exist) or GitHub Releases
    ('vault-media' rolling release) so binary files never bloat the Git repository.
    Returns public CDN URL or empty string if kept locally.
    """
    if not local_path.exists():
        return ""

    # Option A: Cloudflare R2 (10GB free, $0 egress)
    r2_account = os.environ.get("R2_ACCOUNT_ID", "").strip()
    r2_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    r2_secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    r2_bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
    r2_public_url = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")

    if all([r2_account, r2_key, r2_secret, r2_bucket, r2_public_url]):
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                endpoint_url=f"https://{r2_account}.r2.cloudflarestorage.com",
                aws_access_key_id=r2_key,
                aws_secret_access_key=r2_secret,
                region_name="auto"
            )
            content_type = "video/mp4" if local_path.suffix.lower() == ".mp4" else "image/jpeg"
            key_name = f"scenes/{local_path.name}"
            s3.upload_file(
                str(local_path),
                r2_bucket,
                key_name,
                ExtraArgs={"ContentType": content_type}
            )
            cdn_url = f"{r2_public_url}/{key_name}"
            print(f"      ☁️ [R2] Offloaded {local_path.name} -> {cdn_url}")
            return cdn_url
        except Exception as e:
            print(f"      [!] R2 upload notice ({e}), falling back to GitHub Releases...")

    # Option B: GitHub Releases ('vault-media' rolling release tag)
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "hipster-doofus/noir-catalogue").strip()
    if gh_token and gh_repo:
        try:
            headers = {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json"
            }
            tag = "vault-media"
            rel_resp = requests.get(
                f"https://github.com{gh_repo}/releases/tags/{tag}",
                headers=headers,
                timeout=15
            )
            if rel_resp.status_code == 404:
                rel_resp = requests.post(
                    f"https://github.com{gh_repo}/releases",
                    headers=headers,
                    json={
                        "tag_name": tag,
"name": "Noir Vault Media Assets",
"body": "Automated binary storage for extracted 1080p clips and keyframes.",
"draft": False,
"prerelease": False
},
timeout=15
)
if rel_resp.status_code in (200, 201):
rel_data = rel_resp.json()
for asset in rel_data.get("assets", []):
if asset.get("name") == local_path.name:
requests.delete(asset["url"], headers=headers, timeout=15)
upload_base = rel_data["upload_url"].split("{")[0]
mime = "video/mp4" if local_path.suffix.lower() == ".mp4" else "image/jpeg"
with open(local_path, "rb") as f:
up_resp = requests.post(
f"{upload_base}?name={local_path.name}",
headers={**headers, "Content-Type": mime},
data=f,
timeout=120
)
if up_resp.status_code in (200, 201):
dl_url = up_resp.json().get("browser_download_url", "")
print(f"      📦 [GH Release] Offloaded {local_path.name}")
return dl_url
except Exception as e:
print(f"      [!] GitHub Release upload notice ({e}), keeping local file...")
return ""
def extract_json_object(raw_text):
cleaned = raw_text.strip()
if cleaned.startswith(""): cleaned = re.sub(r"^(?:json)?\s*", "", cleaned)
cleaned = re.sub(r"\s*```$", "", cleaned)
match = re.search(r"{.*}", cleaned, re.DOTALL)
if match:
return json.loads(match.group(0))
return json.loads(cleaned)
def get_already_processed_movie_ids():
"""Queries Supabase to locate films that were previously extracted."""
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
print(f"[*] Supabase duplicate check notice ({e}); continuing execution.")
return set()
def fetch_random_archive_candidates(candidate_count=20):
"""Searches the Internet Archive API based on dynamic workflow inputs."""
already_done = get_already_processed_movie_ids()
search_url = "archive.org"
query_parts = ['mediatype:movies', 'format:"MPEG4"']
if ARCHIVE_CATEGORY != "all_movies":
query_parts.append(f'collection:"{ARCHIVE_CATEGORY}"')
if GENRE_KEYWORD != "any":
query_parts.append(f'subject:"{GENRE_KEYWORD}"')
if LICENSE_TYPE != "any":
query_parts.append('(licenseurl:"publicdomain" OR rights:"Public Domain")')
query = " AND ".join(query_parts)
print(f"🔎 Querying Archive API ({SOURCE_PROVIDER} / {ARCHIVE_CATEGORY}) | Keyword: '{GENRE_KEYWORD}'...")
params = {
"q": query,
"fl[]": ["identifier", "title", "year", "creator", "description"],
"sort[]": "downloads desc",
"rows": 50,
"page": random.randint(1, 4),
"output": "json"
}
try:
r = requests.get(search_url, params=params, timeout=20)
r.raise_for_status()
docs = r.json().get("response", {}).get("docs", [])
random.shuffle(docs)
candidates = []
for d in docs:
movie_id = d.get("identifier")
if not movie_id or movie_id in already_done:
continue
raw_year = d.get("year")
try:
year_val = int(raw_year) if raw_year else 1950
except ValueError:
year_val = 1950
if not (START_YEAR <= year_val <= END_YEAR):
continue
meta_url = f"archive.org{movie_id}"
try:
m_resp = requests.get(meta_url, timeout=10)
if m_resp.status_code == 200:
files = m_resp.json().get("files", [])
mp4_files = [
f for f in files
if f.get("name", "").endswith(".mp4") and not f.get("name", "").endswith("_512kb.mp4")
]
if not mp4_files:
mp4_files = [f for f in files if f.get("name", "").endswith(".mp4")]
if mp4_files:
mp4_files.sort(key=lambda x: int(x.get("size", 0)), reverse=True)
best_mp4 = mp4_files[0]["name"]
candidates.append({
"title": d.get("title", movie_id.replace("_", " ").title()),
"year": year_val,
"director": d.get("creator", "Unknown Director"),
"archive_id": movie_id,
"source_provider": SOURCE_PROVIDER,
"video_url": f"archive.org{movie_id}/{best_mp4}",
"license": LICENSE_TYPE.title() if LICENSE_TYPE != "any" else "Open Archive",
"description": d.get("description", "Archival video feature.")
})
except Exception:
continue
if len(candidates) >= candidate_count:
break
print(f"  [✓] Found {len(candidates)} valid candidate films.")
return candidates
except Exception as e:
print(f"  [!] Failed to query Internet Archive API: {e}")
return []
def analyze_multimodal(clip_path, thumb_path, movie_info, scene_index, duration):
"""
Zero-Cost Hybrid Multimodal Analyzer:
1. Local faster-whisper extracts exact spoken dialogue.
2. Local CLIP visually classifies keyframe mood, shot framing, noir tropes & 512-d vector.
3. Optional free-tier text LLM (Gemini 2.5 Flash or HF Router) synthesizes prose.
"""
print(f"    🔍 [Local AI] Running Whisper + CLIP analysis for Scene {scene_index}...")
transcript = transcribe_clip_audio(clip_path)
visual = analyze_keyframe_with_clip(thumb_path)
detected_mood = visual["mood"]
detected_tropes = visual["noir_tropes"]
shot_style = visual["shot_style"]
base_tags = visual["tags"]
clip_embedding = visual["embedding"]
synthesis_prompt = f"""
You are a film noir archivist. Based on the extracted audio transcript and visual computer-vision signals below, write concise cinematic metadata for Scene {scene_index} ({duration:.1f}s) of '{movie_info['title']}' ({movie_info['year']}, dir. {movie_info['director']}).
Extracted Dialogue: "{transcript}"
Detected Visual Framing: {shot_style}
Detected Visual Tropes: {", ".join(detected_tropes)}
Detected Visual Mood: {detected_mood}
Return ONLY valid JSON with these exact keys:
{{
"transcription": "cleaned version of the dialogue above (keep exact meaning)",
"visual_description": "1-2 evocative sentences describing the cinematography, shadows, and framing",
"synopsis": "2 sentence narrative beat summary grounded in the dialogue and visual mood",
"mood": "{detected_mood}",
"noir_tropes": {json.dumps(detected_tropes)},
"tags": {json.dumps(base_tags)}
}}
"""
# Tier 1: Text-only Gemini Free Tier
gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
if gemini_key:
try:
print("    🤖 [Enrich 1/2] Synthesizing prose via Gemini Free Tier (text-only)...")
from google import genai
from google.genai import types
client = genai.Client(api_key=gemini_key)
response = client.models.generate_content(
model="gemini-2.5-flash",
contents=synthesis_prompt,
config=types.GenerateContentConfig(response_mime_type="application/json")
)
parsed = extract_json_object(response.text)
parsed.setdefault("transcription", transcript)
parsed.setdefault("mood", detected_mood)
parsed.setdefault("noir_tropes", detected_tropes)
parsed.setdefault("tags", base_tags)
parsed["clip_embedding"] = clip_embedding
return parsed
except Exception as e:
print(f"    [!] Gemini text synthesis notice ({e}), trying HF Router...")
# Tier 2: Hugging Face Serverless Inference Router
hf_token = os.environ.get("HF_TOKEN", "").strip()
if hf_token:
try:
print("    🤗 [Enrich 2/2] Synthesizing prose via Hugging Face Router...")
headers = {
"Authorization": f"Bearer {hf_token}",
"Content-Type": "application/json"
}
hf_url = "huggingface.co"
payload = {
"model": "Qwen/Qwen2.5-7B-Instruct",
"messages": [{"role": "user", "content": synthesis_prompt}],
"max_tokens": 300,
"temperature": 0.4
}
resp = requests.post(hf_url, headers=headers, json=payload, timeout=20)
if resp.status_code == 200:
content = resp.json()["choices"][0]["message"]["content"]
parsed = extract_json_object(content)
parsed.setdefault("transcription", transcript)
parsed.setdefault("mood", detected_mood)
parsed.setdefault("noir_tropes", detected_tropes)
parsed.setdefault("tags", base_tags)
parsed["clip_embedding"] = clip_embedding
return parsed
except Exception as hf_err:
print(f"    [!] Hugging Face synthesis notice ({hf_err}), using local CLIP+Whisper metadata...")
# Tier 3: Deterministic Local CLIP + Whisper Assembly
tropes_phrase = ", ".join(detected_tropes[:2])
if transcript.startswith("["):
synopsis_text = (
f"An atmospheric {duration:.0f}-second sequence from '{movie_info['title']}' ({movie_info['year']}) "
f"driven by {tropes_phrase} and a {detected_mood.lower()} tone."
)
else:
snippet = transcript if len(transcript) <= 140 else transcript[:137].rsplit(" ", 1)[0] + "..."
synopsis_text = (
f"In a {detected_mood.lower()} exchange from '{movie_info['title']}' ({movie_info['year']}), "
f"dialogue centers on "{snippet}" framed against {tropes_phrase}."
)
return {
"transcription": transcript,
"visual_description": f"Features {shot_style}, accented by {tropes_phrase}.",
"synopsis": synopsis_text,
"mood": detected_mood,
"noir_tropes": detected_tropes,
"tags": base_tags,
"clip_embedding": clip_embedding
}
def index_to_supabase(movie, scenes):
"""Upserts movie metadata, scene details, CDN URLs, and CLIP embeddings into Supabase."""
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
row = {
"scene_id": f"{movie['archive_id']}scene{s['scene_number']}",
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
"download_url": s.get("download_url", ""),
"thumbnail_url": s.get("thumbnail_url", "")
}
if s.get("clip_embedding"):
row["clip_embedding"] = s["clip_embedding"]
client.table("scenes").upsert(row).execute()
return True
except Exception as e:
print(f"❌ Supabase notice: {e}")
return False
def update_catalog_json(all_movies, all_scenes):
"""Merges new batch records with existing history in catalog.json (excluding raw vectors)."""
root_cat = BASE_DIR / "catalog.json"
existing_movies = []
existing_scenes = []
if root_cat.exists():
try:
with open(root_cat, "r", encoding="utf-8") as f:
old_data = json.load(f)
existing_movies = old_data.get("movies", [])
existing_scenes = old_data.get("scenes", [])
except Exception as e:
print(f"[*] Notice loading existing catalog.json ({e})")
movie_dict = {m["archive_id"]: m for m in existing_movies}
for m in all_movies:
movie_dict[m["archive_id"]] = m
scene_dict = {s["scene_id"]: s for s in existing_scenes}
for s in all_scenes:
s_copy = {k: v for k, v in s.items() if k not in ("clip_embedding", "clip_path", "thumb_path")}
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
print(f"✅ Catalog synced! Archive total: {len(combined_movies)} movies & {len(combined_scenes)} scenes.")
def main():
ffmpeg_exe = get_ffmpeg()
movie_pool = fetch_random_archive_candidates(candidate_count=20)
print("=" * 65)
print(f"🎬 TARGET GOAL: Extract Scenes for {NUM_MOVIES} Film(s)")
print(f"📋 Candidate Pool Size: {len(movie_pool)} films")
print("=" * 65)
all_batch_movies = []
all_batch_scenes = []
pool_index = 0
while len(all_batch_movies) < NUM_MOVIES and pool_index < len(movie_pool):
movie = movie_pool[pool_index]
pool_index += 1
print(f"\n[{len(all_batch_movies) + 1}/{NUM_MOVIES}] 📽️ Processing '{movie['title']}' ({movie['year']})...")
sample_file = WORK_DIR / f"{movie['archive_id']}_sample.mp4"
try:
stream_url = movie["video_url"]
random_start_sec = random.randint(300, 1800)
start_time_str = f"{random_start_sec // 3600:02d}:{(random_start_sec % 3600) // 60:02d}:{random_start_sec % 60:02d}"
print(f"  🌐 Fast stream-copying 15-minute sample starting at {start_time_str} (-c copy)...")
t0 = time.time()
# Single-pass stream copy: ~10x faster and preserves master quality
cmd = [
ffmpeg_exe, "-y",
"-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
"-ss", start_time_str,
"-i", stream_url,
"-t", "00:15:00",
"-c", "copy",
str(sample_file)
]
res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
if res.returncode != 0 or not sample_file.exists() or sample_file.stat().st_size < 100000:
print("  [!] Direct FFmpeg stream slice dropped. Using HTTP chunk fallback...")
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
with requests.get(stream_url, headers=headers, stream=True, timeout=30) as r:
r.raise_for_status()
downloaded = 0
with open(sample_file, "wb") as f:
for chunk in r.iter_content(chunk_size=1024 * 1024):
if chunk:
f.write(chunk)
downloaded += len(chunk)
if downloaded >= 80 * 1024 * 1024:
break
if not sample_file.exists() or sample_file.stat().st_size < 100000:
print(f"  [!] Failed to cache video sample for '{movie['title']}'. Moving on...")
continue
print(f"  [✓] Sample cached in {time.time() - t0:.1f}s ({sample_file.stat().st_size / (1024 * 1024):.1f} MB)")
from scenedetect import open_video, SceneManager, AdaptiveDetector
print("  🔍 Detecting scene boundaries...")
video = open_video(str(sample_file))
sm = SceneManager()
sm.add_detector(AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=24))
sm.detect_scenes(video, frame_skip=2)
scene_list = sm.get_scene_list()
del video
gc.collect()
scored_scenes = [
(st, en, en.get_seconds() - st.get_seconds())
for st, en in scene_list
]
valid = [
(st, en, dur) for st, en, dur in scored_scenes
if MIN_CLIP_LEN <= dur <= MAX_CLIP_LEN
]
if len(valid) < CLIPS_PER_MOVIE:
valid = [
(st, en, dur) for st, en, dur in scored_scenes
if dur >= 3.0
][:CLIPS_PER_MOVIE]
if not valid:
print(f"  [!] No valid scene cuts detected for '{movie['title']}'. Skipping...")
continue
chosen = random.sample(valid, min(CLIPS_PER_MOVIE, len(valid)))
chosen.sort(key=lambda s: s[0].get_seconds())
movie_scenes = []
for idx, (st, en, dur) in enumerate(chosen, 1):
clip_name = f"{movie['archive_id']}scene{idx}_{int(dur)}s_1080p.mp4"
thumb_name = f"{movie['archive_id']}scene{idx}_thumb.jpg"
clip_path = OUTPUT_DIR / clip_name
thumb_path = OUTPUT_DIR / thumb_name
transcode_cmd = [
ffmpeg_exe, "-y",
"-ss", st.get_timecode(),
"-to", en.get_timecode(),
"-i", str(sample_file),
"-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
"-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
"-c:v", "libx264", "-preset", "fast", "-crf", "19",
"-c:a", "aac", "-b:a", "192k",
"-fflags", "+genpts", "-movflags", "+faststart",
str(clip_path)
]
subprocess.run(transcode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
mid_sec = (st.get_seconds() + en.get_seconds()) / 2.0
thumb_cmd = [
ffmpeg_exe, "-y",
"-ss", f"{mid_sec:.3f}",
"-i", str(sample_file),
"-vframes", "1", "-q:v", "2",
str(thumb_path)
]
subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
meta = analyze_multimodal(clip_path, thumb_path, movie, idx, dur)
# Offload binary assets to Cloudflare R2 / GitHub Releases & prune local copies
video_cdn_url = offload_asset_to_cloud(clip_path)
thumb_cdn_url = offload_asset_to_cloud(thumb_path)
if video_cdn_url:
clip_path.unlink(missing_ok=True)
if thumb_cdn_url:
thumb_path.unlink(missing_ok=True)
abs_start = random_start_sec + st.get_seconds()
abs_end = random_start_sec + en.get_seconds()
abs_tc_start = f"{int(abs_start) // 3600:02d}:{(int(abs_start) % 3600) // 60:02d}:{int(abs_start) % 60:02d}"
abs_tc_end = f"{int(abs_end) // 3600:02d}:{(int(abs_end) % 3600) // 60:02d}:{int(abs_end) % 60:02d}"
scene_record = {
"scene_id": f"{movie['archive_id']}scene{idx}",
"movie_id": movie["archive_id"],
"scene_number": idx,
"start_timecode": abs_tc_start,
"end_timecode": abs_tc_end,
"start_seconds": abs_start,
"end_seconds": abs_end,
"duration_seconds": dur,
"video_filename": clip_name,
"keyframe_filename": thumb_name,
"download_url": video_cdn_url,
"thumbnail_url": thumb_cdn_url,
**meta
}
movie_scenes.append(scene_record)
all_batch_scenes.append(scene_record)
index_to_supabase(movie, movie_scenes)
all_batch_movies.append(movie)
print(f"  [✓] Successfully processed '{movie['title']}' ({len(all_batch_movies)}/{NUM_MOVIES})")
except Exception as e:
print(f"  [!] Exception during extraction of {movie['title']}: {e}")
finally:
gc.collect()
if sample_file.exists():
sample_file.unlink(missing_ok=True)
if not all_batch_movies:
print("[!] No candidate movies were successfully processed.")
return
# Update local SQLite database
sqlite_db_path = OUTPUT_DIR / "film_noir_scenes.db"
conn = sqlite3.connect(sqlite_db_path)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS movies (
movie_id TEXT PRIMARY KEY,
title TEXT,
director TEXT,
release_year INTEGER,
source_archive_url TEXT
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS scenes (
scene_id TEXT PRIMARY KEY,
movie_id TEXT,
scene_number INTEGER,
start_seconds REAL,
end_seconds REAL,
duration_seconds REAL,
start_timecode TEXT,
end_timecode TEXT,
transcription TEXT,
visual_description TEXT,
synopsis TEXT,
mood TEXT,
noir_tropes TEXT,
tags TEXT,
keyframe_filename TEXT,
video_filename TEXT,
download_url TEXT,
thumbnail_url TEXT
);
""")
for col in ("download_url", "thumbnail_url"):
try:
cur.execute(f"ALTER TABLE scenes ADD COLUMN {col} TEXT;")
except sqlite3.OperationalError:
pass
for m in all_batch_movies:
cur.execute(
"INSERT OR REPLACE INTO movies VALUES (?, ?, ?, ?, ?)",
(m["archive_id"], m["title"], m["director"], m["year"], m["video_url"])
)
for s in all_batch_scenes:
cur.execute(
"INSERT OR REPLACE INTO scenes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
(
s["scene_id"], s["movie_id"], s["scene_number"],
s["start_seconds"], s["end_seconds"], s["duration_seconds"],
s["start_timecode"], s["end_timecode"],
s["transcription"], s["visual_description"], s["synopsis"], s["mood"],
json.dumps(s.get("noir_tropes", [])),
json.dumps(s.get("tags", [])),
s["keyframe_filename"], s["video_filename"],
s.get("download_url", ""), s.get("thumbnail_url", "")
)
)
conn.commit()
conn.close()
update_catalog_json(all_batch_movies, all_batch_scenes)
print("=" * 65)
print(f"🎉 BATCH COMPLETE! Extracted {len(all_batch_movies)} film(s) & {len(all_batch_scenes)} clip(s).")
print("=" * 65)
if name == "main":
main()