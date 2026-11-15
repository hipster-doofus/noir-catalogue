#!/usr/bin/env python3
"""
One-Time Cloudflare R2 Media Migrator
=====================================
Uploads existing local MP4 clips and JPG thumbnails in output/ to Cloudflare R2,
backfills download_url and thumbnail_url in Supabase, SQLite, and catalog.json,
and optionally removes migrated local MP4 binaries so Git stays lightweight.
"""
import os
import sys
import json
import sqlite3
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
ROOT_CATALOG = BASE_DIR / "catalog.json"
OUT_CATALOG = OUTPUT_DIR / "catalog.json"
SQLITE_DB = OUTPUT_DIR / "film_noir_scenes.db"


def get_r2_client():
    import boto3
    from botocore.config import Config

    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
    public_url = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")

    if not all([account_id, access_key, secret_key, bucket, public_url]):
        raise RuntimeError(
            "Missing required R2 environment variables: "
            "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL"
        )

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    return client, bucket, public_url


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def upload_file(s3, bucket: str, public_url: str, local_path: Path, key: str, content_type: str, dry_run: bool) -> str:
    cdn_url = f"{public_url}/{key}"
    if dry_run:
        print(f"  [DRY RUN] Would upload {local_path.name} -> {cdn_url}")
        return cdn_url

    if object_exists(s3, bucket, key):
        print(f"  [⏭️ Exists] {key} already in R2")
        return cdn_url

    size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"  [⬆️ Uploading] {local_path.name} ({size_mb:.2f} MB) -> {key}")
    s3.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )
    return cdn_url


def main():
    parser = argparse.ArgumentParser(description="Migrate existing output/ MP4s and thumbnails to Cloudflare R2.")
    parser.add_argument("--keep-local", action="store_true", help="Keep local MP4 files instead of deleting after upload")
    parser.add_argument("--dry-run", action="store_true", help="Preview uploads and URL updates without modifying anything")
    args = parser.parse_args()

    if not OUTPUT_DIR.exists():
        print("Error: output/ directory does not exist.")
        sys.exit(1)

    s3, bucket, public_url = get_r2_client()
    print(f"Connected to Cloudflare R2 bucket '{bucket}' ({public_url})")

    mp4_files = sorted(OUTPUT_DIR.glob("*.mp4"))
    jpg_files = sorted(OUTPUT_DIR.glob("*.jpg"))
    print(f"Found {len(mp4_files)} MP4 clip(s) and {len(jpg_files)} JPG thumbnail(s) in output/.
")

    uploaded_videos = {}
    uploaded_thumbs = {}

    for jpg in jpg_files:
        url = upload_file(s3, bucket, public_url, jpg, f"thumbs/{jpg.name}", "image/jpeg", args.dry_run)
        uploaded_thumbs[jpg.name] = url

    for mp4 in mp4_files:
        url = upload_file(s3, bucket, public_url, mp4, f"clips/{mp4.name}", "video/mp4", args.dry_run)
        uploaded_videos[mp4.name] = url

    # Update catalog.json (both root and output/)
    updated_scenes = 0
    for cat_path in [ROOT_CATALOG, OUT_CATALOG]:
        if not cat_path.exists():
            continue
        with open(cat_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        scenes = data.get("scenes", []) if isinstance(data, dict) else data
        for sc in scenes:
            v_name = sc.get("video_filename") or sc.get("clip_filename") or ""
            t_name = sc.get("keyframe_filename") or sc.get("thumb_filename") or ""
            if v_name in uploaded_videos:
                sc["download_url"] = uploaded_videos[v_name]
                updated_scenes += 1
            if t_name in uploaded_thumbs:
                sc["thumbnail_url"] = uploaded_thumbs[t_name]

        if not args.dry_run:
            with open(cat_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        print(f"Updated {cat_path.name} with R2 CDN URLs.")

    # Update SQLite database if present
    if SQLITE_DB.exists() and not args.dry_run:
        conn = sqlite3.connect(SQLITE_DB)
        cur = conn.cursor()
        cols = {row[1] for row in cur.execute("PRAGMA table_info(scenes)")}
        if "download_url" not in cols:
            cur.execute("ALTER TABLE scenes ADD COLUMN download_url TEXT")
        if "thumbnail_url" not in cols:
            cur.execute("ALTER TABLE scenes ADD COLUMN thumbnail_url TEXT")

        for v_name, v_url in uploaded_videos.items():
            cur.execute("UPDATE scenes SET download_url = ? WHERE video_filename = ?", (v_url, v_name))
        for t_name, t_url in uploaded_thumbs.items():
            cur.execute("UPDATE scenes SET thumbnail_url = ? WHERE keyframe_filename = ?", (t_url, t_name))
        conn.commit()
        conn.close()
        print("Updated SQLite database (film_noir_scenes.db) with R2 CDN URLs.")

    # Update Supabase scenes table
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = os.environ.get("SUPABASE_KEY", "").strip()
    if sb_url and sb_key and not args.dry_run:
        from supabase import create_client
        sb = create_client(sb_url, sb_key)
        for v_name, v_url in uploaded_videos.items():
            payload = {"download_url": v_url}
            thumb_candidate = v_name.rsplit("_", 2)[0] + "_thumb.jpg" if "_scene_" in v_name else v_name.replace(".mp4", "_thumb.jpg")
            if thumb_candidate in uploaded_thumbs:
                payload["thumbnail_url"] = uploaded_thumbs[thumb_candidate]
            sb.table("scenes").update(payload).eq("video_filename", v_name).execute()
        print("Backfilled Supabase scenes table with R2 CDN URLs.")

    # Remove local MP4 binaries once safely uploaded
    if not args.keep_local and not args.dry_run:
        removed = 0
        for mp4 in mp4_files:
            if mp4.name in uploaded_videos and mp4.exists():
                mp4.unlink()
                removed += 1
        print(f"Removed {removed} local MP4 file(s) from output/.")

    print("
🎉 Cloudflare R2 migration complete!")


if __name__ == "__main__":
    main()
