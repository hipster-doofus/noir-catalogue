#!/usr/bin/env python3
"""
One-Time Cloudflare R2 Media Migration Script
=============================================
Uploads existing .mp4 clips and _thumb.jpg keyframes from output/ to Cloudflare R2,
backfills download_url and thumbnail_url in catalog.json, SQLite, and Supabase,
and deletes local .mp4 binaries so Git stays lightweight.
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
    bucket_name = os.environ.get("R2_BUCKET_NAME", "").strip()
    public_url = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")

    if not all([account_id, access_key, secret_key, bucket_name, public_url]):
        raise RuntimeError(
            "Missing required R2 environment variables: "
            "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL"
        )

    client = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return client, bucket_name, public_url


def object_exists(s3_client, bucket_name: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3_client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError:
        return False


def upload_file(s3_client, bucket_name: str, public_url: str, local_path: Path, key: str, content_type: str) -> str:
    if not object_exists(s3_client, bucket_name, key):
        s3_client.upload_file(
            Filename=str(local_path),
            Bucket=bucket_name,
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
    return f"{public_url}/{key}"


def main():
    parser = argparse.ArgumentParser(description="Migrate existing output/ media to Cloudflare R2.")
    parser.add_argument("--dry-run", action="store_true", help="Preview uploads and DB updates without modifying files.")
    parser.add_argument("--keep-local", action="store_true", help="Keep local MP4 files after uploading to R2.")
    args = parser.parse_args()

    if not OUTPUT_DIR.exists():
        print("output/ directory does not exist. Nothing to migrate.")
        return

    mp4_files = sorted(OUTPUT_DIR.glob("*.mp4"))
    jpg_files = sorted(OUTPUT_DIR.glob("*.jpg"))
    print(f"Found {len(mp4_files)} MP4 clip(s) and {len(jpg_files)} JPG thumbnail(s) in output/.")

    if not mp4_files and not jpg_files:
        print("No media files found in output/. Nothing to migrate.")
        return

    if args.dry_run:
        public_url = os.environ.get("R2_PUBLIC_URL", "https://r2.dev").strip().rstrip("/")
        for f in mp4_files[:10]:
            print(f"  [DRY RUN] Would upload {f.name} -> {public_url}/clips/{f.name}")
        for f in jpg_files[:10]:
            print(f"  [DRY RUN] Would upload {f.name} -> {public_url}/thumbs/{f.name}")
        print("Dry run complete. No files uploaded or deleted.")
        return

    s3_client, bucket_name, public_url = get_r2_client()
    video_url_map = {}
    thumb_url_map = {}

    # 1. Upload thumbnails
    for idx, jpg in enumerate(jpg_files, 1):
        key = f"thumbs/{jpg.name}"
        url = upload_file(s3_client, bucket_name, public_url, jpg, key, "image/jpeg")
        thumb_url_map[jpg.name] = url
        if idx % 25 == 0 or idx == len(jpg_files):
            print(f"  [Thumbnails] {idx}/{len(jpg_files)} synced to R2...")

    # 2. Upload MP4 clips
    for idx, mp4 in enumerate(mp4_files, 1):
        key = f"clips/{mp4.name}"
        url = upload_file(s3_client, bucket_name, public_url, mp4, key, "video/mp4")
        video_url_map[mp4.name] = url
        if idx % 10 == 0 or idx == len(mp4_files):
            print(f"  [Videos] {idx}/{len(mp4_files)} synced to R2...")

    # 3. Update catalog.json (both root and output/)
    updated_catalog_scenes = 0
    for cat_path in (ROOT_CATALOG, OUT_CATALOG):
        if not cat_path.exists():
            continue
        try:
            with open(cat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenes = data.get("scenes", []) if isinstance(data, dict) else data
            for sc in scenes:
                v_name = sc.get("video_filename") or sc.get("clip_filename") or ""
                t_name = sc.get("keyframe_filename") or sc.get("thumb_filename") or ""
                if v_name in video_url_map:
                    sc["download_url"] = video_url_map[v_name]
                    if cat_path == ROOT_CATALOG:
                        updated_catalog_scenes += 1
                if t_name in thumb_url_map:
                    sc["thumbnail_url"] = thumb_url_map[t_name]
                sc.pop("clip_path", None)
                sc.pop("thumb_path", None)
            with open(cat_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  [!] Notice updating {cat_path.name}: {e}")

    print(f"Updated {updated_catalog_scenes} scene record(s) in catalog.json.")

    # 4. Update local SQLite database if present
    if SQLITE_DB.exists():
        try:
            conn = sqlite3.connect(SQLITE_DB)
            cur = conn.cursor()
            cols = {row[1] for row in cur.execute("PRAGMA table_info(scenes)").fetchall()}
            if "download_url" not in cols:
                cur.execute("ALTER TABLE scenes ADD COLUMN download_url TEXT")
            if "thumbnail_url" not in cols:
                cur.execute("ALTER TABLE scenes ADD COLUMN thumbnail_url TEXT")
            for v_name, v_url in video_url_map.items():
                cur.execute("UPDATE scenes SET download_url = ? WHERE video_filename = ?", (v_url, v_name))
            for t_name, t_url in thumb_url_map.items():
                cur.execute("UPDATE scenes SET thumbnail_url = ? WHERE keyframe_filename = ?", (t_url, t_name))
            conn.commit()
            conn.close()
            print("Updated local SQLite database with R2 URLs.")
        except Exception as e:
            print(f"  [!] Notice updating SQLite DB: {e}")

    # 5. Update Supabase scenes table
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if supabase_url and supabase_key:
        try:
            from supabase import create_client

            sb = create_client(supabase_url, supabase_key)
            sb_updated = 0
            for v_name, v_url in video_url_map.items():
                payload = {"download_url": v_url}
                t_guess = v_name.rsplit("_", 2)[0] + "_thumb.jpg" if "_scene_" in v_name else ""
                if t_guess in thumb_url_map:
                    payload["thumbnail_url"] = thumb_url_map[t_guess]
                sb.table("scenes").update(payload).eq("video_filename", v_name).execute()
                sb_updated += 1
            for t_name, t_url in thumb_url_map.items():
                sb.table("scenes").update({"thumbnail_url": t_url}).eq("keyframe_filename", t_name).execute()
            print(f"Synced {sb_updated} scene URL(s) to Supabase.")
        except Exception as e:
            print(f"  [!] Notice updating Supabase: {e}")

    # 6. Remove local MP4s once safely uploaded unless --keep-local is specified
    if not args.keep_local:
        removed = 0
        for mp4 in mp4_files:
            try:
                mp4.unlink(missing_ok=True)
                removed += 1
            except Exception as e:
                print(f"  [!] Could not remove {mp4.name}: {e}")
        print(f"Removed {removed} migrated MP4 file(s) from output/.")

    print("Migration to Cloudflare R2 complete!")


if __name__ == "__main__":
    main()
