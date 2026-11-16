# Noir Vault — Architecture & Session Handoff Context

## Project Overview
- **Repo:** `hipster-doofus/noir-catalogue` (default branch: `main`, live frontend: `https://workers.dev` and `https://pages.dev`).
- **Stack:** Python (FFmpeg `-c copy` slicing, `scenedetect`, `faster-whisper`, local `openai/clip-vit-base-patch32`), GitHub Actions, Cloudflare R2 (MP4 & JPG storage), Cloudflare Pages/Workers (static frontend), and Supabase PostgreSQL (`pgvector` + FTS).

## Completed Changes
1. **Core Extraction Pipeline (`extract_and_index.py` & `.github/workflows/extract_scenes.yml`):**
   - Extracts scenes from Internet Archive / blended sources, runs local Whisper + CLIP, offloads `.mp4` clips to `R2_BUCKET_NAME/clips/` and thumbnails to `R2_BUCKET_NAME/thumbs/`, updates `catalog.json` and 18-column `output/film_noir_scenes.db`, and upserts to Supabase `movies` and `scenes` (`download_url`, `thumbnail_url`).
2. **Static Hosting Deployment (`.github/workflows/deploy_static_vault.yml`):**
   - Uses `sparse-checkout` to package lightweight HTML files (`index.html`, `social.html`, `studio.html`, `vj_scratch.html`) and `catalog.json` into `_site/` and deploys to Cloudflare Pages/Workers (plus optional Netlify, Vercel, Render).
3. **Add-On Multi-Model AI Re-Analyzer & R2 DB Reconciliation:**
   - `schema_addons.sql`: Adds `public.scenes_archive` and `public.scene_analyses` (stores multi-model comparison runs per `scene_id`).
   - `cleanup_orphaned_db.py`: Lists all keys in Cloudflare R2, moves Supabase `scenes` rows without matching R2 files into `scenes_archive`, backfills missing R2 URLs, and syncs `catalog.json`.
   - `analyze_existing_clips.py` & `.github/workflows/analyze_and_reconcile.yml`: Standalone workflow (scheduled every 6 hours + `workflow_dispatch` with model/prompt/temperature dropdowns) to ingest local clips (`ingest_clips/`) or re-analyze R2 clips with `gemini-2.5-flash` (3 keyframes + Whisper transcript to stay inside free-tier quotas, stopping cleanly on HTTP `429`), `gemini-2.5-pro`, `hf-qwen`, or `local-clip-whisper`.
4. **Cloudflare R2 CORS Configuration (`r2_cors_policy.json`):**
   - Prepared CORS rule allowing `GET` & `HEAD` methods with exposed headers (`Content-Length`, `Content-Range`, `Accept-Ranges`) for web player streaming & Blob downloads.
5. **Frontend Updates (`index.html` & `social.html`):**
   - Cross-origin `<a download>` links replaced with asynchronous `fetch(url) -> Blob -> URL.createObjectURL(blob)` `downloadClip()` helper with button loading states.
   - External `via.placeholder.com` fallbacks replaced with a standalone inline dark-gold film noir SVG data URI.
   - AI model comparison dropdown implemented inside the video modal, allowing dynamic side-by-side inspection between primary Whisper+CLIP extractions and multi-model runs stored in `public.scene_analyses`.

## Next Operational Steps
1. **Apply Supabase Schema Addons:**
   - Run the SQL in [schema_addons.sql](file:///c:/Users/me/.gemini/antigravity/scratch/noir-catalogue/schema_addons.sql) in your Supabase SQL Editor to create `public.scenes_archive` and `public.scene_analyses`.
2. **Apply Cloudflare R2 CORS Policy:**
   - In Cloudflare Dashboard -> R2 -> Select Bucket -> Settings -> CORS Policy, paste the contents of [r2_cors_policy.json](file:///c:/Users/me/.gemini/antigravity/scratch/noir-catalogue/r2_cors_policy.json).
3. **Run DB Reconciliation Workflow:**
   - In GitHub -> Actions -> "Analyze Existing Clips & Reconcile DB" -> Run workflow -> Select `cleanup_orphaned_db` (or `cleanup_orphaned_db_dry_run`).
