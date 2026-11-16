#!/usr/bin/env python3
"""
Supabase <-> Cloudflare R2 Reconciliation & Orphan Archiver
==========================================================
Scans your Cloudflare R2 bucket for all existing MP4 clips, compares against
Supabase public.scenes, moves missing/orphaned records to public.scenes_archive,
removes them from public.scenes, and updates catalog.json so the web UI only
displays clips that actually exist in R2.
"""
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
ROOT_CATALOG = BASE_DIR / "catalog.json"
OUT_CATALOG = BASE_DIR / "output" / "catalog.json"


def list_all_r2_keys():
    import boto3
    from botocore.config import Config

    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket_name = os.environ.get("R2_BUCKET_NAME", "").strip()
    public_url = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")

    if not all([account_id, access_key, secret_key, bucket_name]):
        raise RuntimeError("Missing R2 credentials in environment variables.")

    s3 = boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    paginator = s3.get_paginator("list_objects_v2")
    r2_filenames = set()
    for page in paginator.paginate(Bucket=bucket_name):
        for obj in page.get("Contents", []):
            r2_filenames.add(Path(obj["Key"]).name)

    return r2_filenames, public_url


def main():
    parser = argparse.ArgumentParser(description="Archive Supabase scenes that have no matching file in R2.")
    parser.add_argument("--dry-run", action="store_true", help="Report orphaned records without modifying Supabase.")
    parser.add_argument("--hard-delete", action="store_true", help="Delete without copying to scenes_archive.")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY.")

    from supabase import create_client
    sb = create_client(supabase_url, supabase_key)

    print("Listing all files currently stored in Cloudflare R2...")
    r2_filenames, public_url = list_all_r2_keys()
    print(f"  [OK] Found {len(r2_filenames)} unique file(s) in R2.")

    all_rows = []
    offset = 0
    while True:
        res = sb.table("scenes").select("*").range(offset, offset + 499).execute()
        batch = res.data or []
        all_rows.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    print(f"  [OK] Found {len(all_rows)} scene record(s) in Supabase.")

    orphans = []
    valid_rows = []
    backfill_updates = []

    for row in all_rows:
        v_file = (row.get("video_filename") or "").strip()
        dl_url = (row.get("download_url") or "").strip()
        url_basename = Path(dl_url.split("?")[0]).name if dl_url else ""

        exists_in_r2 = (v_file and v_file in r2_filenames) or (url_basename and url_basename in r2_filenames)
        if exists_in_r2:
            valid_rows.append(row)
            if public_url and v_file and (not dl_url or not dl_url.startswith("http")):
                t_file = (row.get("keyframe_filename") or "").strip()
                upd = {"download_url": f"{public_url}/clips/{v_file}"}
                if t_file and t_file in r2_filenames:
                    upd["thumbnail_url"] = f"{public_url}/thumbs/{t_file}"
                backfill_updates.append((row["scene_id"], upd))
        else:
            orphans.append(row)

    print(f"Valid R2 scenes: {len(valid_rows)} | Backfills: {len(backfill_updates)} | Orphans: {len(orphans)}")

    if args.dry_run:
        for o in orphans[:15]:
            print(f"  [ORPHAN] {o.get('scene_id')} -> {o.get('video_filename')}")
        return

    for scene_id, upd in backfill_updates:
        sb.table("scenes").update(upd).eq("scene_id", scene_id).execute()

    if orphans:
        if not args.hard_delete:
            archive_payloads = []
            for o in orphans:
                copy_row = {k: v for k, v in o.items() if k != "search_tsv"}
                copy_row["archive_reason"] = "missing_in_r2"
                archive_payloads.append(copy_row)
            for i in range(0, len(archive_payloads), 100):
                sb.table("scenes_archive").upsert(archive_payloads[i:i + 100]).execute()

        orphan_ids = [o["scene_id"] for o in orphans]
        for i in range(0, len(orphan_ids), 100):
            sb.table("scenes").delete().in_("scene_id", orphan_ids[i:i + 100]).execute()

    valid_ids = {r["scene_id"] for r in valid_rows}
    for cat_path in (ROOT_CATALOG, OUT_CATALOG):
        if not cat_path.exists():
            continue
        with open(cat_path, "r", encoding="utf-8") as f:
            cat = json.load(f)
        if isinstance(cat, dict) and "scenes" in cat:
            cat["scenes"] = [s for s in cat["scenes"] if s.get("scene_id") in valid_ids]
            cat["total_scenes"] = len(cat["scenes"])
            cat["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(cat_path, "w", encoding="utf-8") as f:
                json.dump(cat, f, indent=2)
    print("Done! Synced catalog.json with verified R2 scenes.")


if __name__ == "__main__":
    main()
