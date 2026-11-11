#!/usr/bin/env python3
"""
Remote to Odysee Uploader
========================
Upload videos to Odysee directly from GitHub, Google Drive, or any remote URL
WITHOUT downloading or saving video files to your local hard drive.

Supports two zero-disk methods:
1. Native Odysee Cloud Ingestion (--mode odysee-cloud):
   Sends the remote URL directly to Odysee's `/api/v1/asynqueries/urls/` endpoint.
   Odysee's servers download the video directly from GitHub / remote server.
   Uses 0 bytes of local disk space and 0 bytes of local upload bandwidth.

2. In-Memory Streaming Pipe (--mode in-memory-stream):
   Used for sources requiring authentication or session cookies (like Google Drive).
   Streams small 8MB memory chunks directly from Google Drive into Odysee's Tus API.
   The video passes directly through RAM; no files are ever written to the local disk.

Supports:
- Single URL / Google Drive link via CLI
- Batch file (JSON, CSV, or TXT list of URLs with titles, descriptions, and tags)
- Automatic playlist (collection) creation
- Can be run locally OR in GitHub Actions (cloud runner)
"""

import os
import sys
import re
import json
import time
import argparse
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import requests

LBRY_PROXY_URL = "https://api.na-backend.odysee.com/api/v1/proxy"
ODYSEE_V4_API = "https://api.na-backend.odysee.com/api/v1/asynqueries"
ODYSEE_V4_INIT_UPLOAD = f"{ODYSEE_V4_API}/uploads/"
ODYSEE_V4_INIT_URL = f"{ODYSEE_V4_API}/urls/"
ODYSEE_THUMBNAIL_CDN_UPLOAD = "https://thumbs.odycdn.com/upload"
CHUNK_SIZE_DEFAULT = 8 * 1024 * 1024  # 8 MB chunks in memory


def sanitize_claim_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\-]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = f"remote-{int(time.time())}"
    return s[:40]


class GoogleDriveStreamer:
    """Resolves and streams Google Drive files directly without saving to disk."""

    @staticmethod
    def extract_file_id(url_or_id: str) -> str:
        """Extract Google Drive file ID from URL or return raw ID."""
        patterns = [
            r"/file/d/([a-zA-Z0-9_-]+)",
            r"id=([a-zA-Z0-9_-]+)",
            r"/d/([a-zA-Z0-9_-]+)",
        ]
        for p in patterns:
            m = re.search(p, url_or_id)
            if m:
                return m.group(1)
        return url_or_id.strip()

    @classmethod
    def open_stream(cls, file_id_or_url: str) -> Tuple[requests.Response, str, int]:
        """Open a streaming HTTP connection to a Google Drive file handling confirmation tokens."""
        file_id = cls.extract_file_id(file_id_or_url)
        session = requests.Session()
        base_url = "https://drive.google.com/uc?export=download"

        resp = session.get(base_url, params={"id": file_id}, stream=True, timeout=30)
        resp.raise_for_status()

        # Handle virus scan confirmation for files > 100MB
        for key, value in resp.cookies.items():
            if key.startswith("download_warning"):
                resp = session.get(base_url, params={"id": file_id, "confirm": value}, stream=True, timeout=30)
                break

        # Check content-disposition for filename
        cd = resp.headers.get("content-disposition", "")
        filename = "gdrive_video.mp4"
        if "filename=" in cd:
            m = re.search(r'filename="?([^";]+)"?', cd)
            if m:
                filename = m.group(1)

        size = int(resp.headers.get("content-length", 0))
        return resp, filename, size


class RemoteOdyseeUploader:
    """Handles remote URL handoff or memory-streaming upload to Odysee."""

    def __init__(self, auth_token: str):
        self.auth_token = auth_token.strip()
        self.session = requests.Session()
        self.session.headers.update({
            "X-Lbry-Auth-Token": self.auth_token,
            "User-Agent": "RemoteOdyseeUploader/1.0",
        })

    def list_channels(self) -> List[Dict[str, Any]]:
        payload = {
            "jsonrpc": "2.0",
            "method": "channel_list",
            "params": {"page": 1, "page_size": 50},
            "id": int(time.time()),
        }
        resp = self.session.post(LBRY_PROXY_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Odysee Error: {data['error'].get('message', data['error'])}")
        items = data.get("result", {}).get("items", [])
        return [{"claim_id": c["claim_id"], "name": c["name"], "title": c.get("value", {}).get("title") or c["name"]} for c in items]

    def upload_thumbnail_from_url(self, thumb_url: str) -> Optional[str]:
        """Fetch remote image into memory and upload to Odysee CDN."""
        try:
            r = requests.get(thumb_url, timeout=30)
            r.raise_for_status()
            files = {"file-input": ("thumbnail.jpg", r.content, "image/jpeg")}
            data = {"upload": "Upload"}
            cdn_res = requests.post(ODYSEE_THUMBNAIL_CDN_UPLOAD, files=files, data=data, timeout=60)
            cdn_data = cdn_res.json()
            return cdn_data.get("url") or cdn_data.get("message")
        except Exception as e:
            print(f"      [Thumb] Warning: Could not upload thumbnail from URL {thumb_url}: {e}")
            return thumb_url

    # ----------------------------------------------------
    # Method 1: Native Remote URL (Odysee Cloud Ingestion)
    # ----------------------------------------------------
    def publish_via_remote_url(self, remote_url: str, claim_params: Dict[str, Any]) -> Dict[str, Any]:
        """Instruct Odysee servers to download the file directly from the remote URL."""
        print(f"      [Cloud Ingest] Requesting Odysee cloud ingestion for: {remote_url[:60]}...")
        # 1. Get URL token
        resp = self.session.post(ODYSEE_V4_INIT_URL, json={}, timeout=30)
        resp.raise_for_status()
        t_data = resp.json()
        if t_data.get("status") != "upload_token_created":
            raise RuntimeError(f"Token creation failed: {t_data}")

        upload_token = t_data["payload"]["token"]
        upload_location = t_data["payload"]["location"]

        # 2. Tell Odysee where to fetch the video
        start_res = requests.post(
            upload_location,
            headers={"Authorization": f"Bearer {upload_token}", "Content-Type": "application/json"},
            json={"url": remote_url},
            timeout=60
        )
        start_res.raise_for_status()
        s_data = start_res.json()
        upload_id = s_data.get("payload", {}).get("upload_id")
        if not upload_id:
            raise RuntimeError(f"Failed to start remote ingestion: {s_data}")

        sdk_file_path = f"{upload_location}{upload_id}"
        print(f"      [Cloud Ingest] Remote ingestion registered (ID: {upload_id})")

        # 3. Create claim
        claim_params["file_path"] = sdk_file_path
        return self._create_and_poll_claim(claim_params)

    # ----------------------------------------------------
    # Method 2: In-Memory Streaming Pipe (Zero Disk Space)
    # ----------------------------------------------------
    def publish_via_in_memory_stream(
        self,
        stream_response: requests.Response,
        filename: str,
        file_size: int,
        claim_params: Dict[str, Any],
        chunk_size: int = CHUNK_SIZE_DEFAULT
    ) -> Dict[str, Any]:
        """Stream chunks directly through RAM into Odysee's Tus API (zero bytes written to disk)."""
        print(f"      [RAM Stream] Initializing Tus session for {filename} ({file_size / (1024*1024):.2f} MB)...")

        # 1. Get Tus token
        resp = self.session.post(ODYSEE_V4_INIT_UPLOAD, json={}, timeout=30)
        resp.raise_for_status()
        t_data = resp.json()
        upload_token = t_data["payload"]["token"]
        upload_location = t_data["payload"]["location"]

        import base64
        b64_filename = base64.b64encode(filename.encode()).decode()
        b64_filetype = base64.b64encode(b"video/mp4").decode()

        init_headers = {
            "Authorization": f"Bearer {upload_token}",
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
            "Upload-Metadata": f"filename {b64_filename},filetype {b64_filetype}",
        }
        res = requests.post(upload_location, headers=init_headers, timeout=60)
        if res.status_code not in (201, 200):
            raise RuntimeError(f"Tus init failed ({res.status_code}): {res.text}")

        upload_url = res.headers.get("Location")
        if not upload_url.startswith("http"):
            from urllib.parse import urljoin
            upload_url = urljoin(upload_location, upload_url)

        # 2. Stream chunks through memory
        offset = 0
        start_time = time.time()
        print("      [RAM Stream] Streaming data directly from source to Odysee (no disk cache)...")

        for chunk in stream_response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue

            patch_headers = {
                "Authorization": f"Bearer {upload_token}",
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            }
            p_res = requests.patch(upload_url, headers=patch_headers, data=chunk, timeout=120)
            if p_res.status_code in (204, 200):
                offset += len(chunk)
                elapsed = time.time() - start_time
                speed = (offset / (1024 * 1024)) / (elapsed + 0.001)
                pct = (offset / file_size) * 100 if file_size else 0
                sys.stdout.write(f"\r      [RAM Stream] Streamed: {pct:5.1f}% [{offset//(1024*1024):3d}MB/{file_size//(1024*1024):3d}MB] at {speed:4.2f} MB/s")
                sys.stdout.flush()
            else:
                raise RuntimeError(f"Tus chunk streaming failed at offset {offset}: {p_res.text}")

        print(f"\n      [RAM Stream] Finished streaming in {time.time() - start_time:.1f}s!")

        # 3. Create claim
        claim_params["file_path"] = upload_url
        return self._create_and_poll_claim(claim_params)

    def _create_and_poll_claim(self, claim_params: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "method": "stream_create",
            "params": claim_params,
            "id": int(time.time()),
        }
        resp = self.session.post(f"{ODYSEE_V4_API}/", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        query_id = data.get("payload", {}).get("query_id")
        if not query_id:
            raise RuntimeError(f"Claim submission failed: {data}")

        print(f"      [Publish] Query ID: {query_id}. Waiting for confirmation...")
        poll_url = f"{ODYSEE_V4_API}/{query_id}"
        deadline = time.time() + (15 * 60)
        while time.time() < deadline:
            poll_resp = self.session.get(poll_url, timeout=30)
            if poll_resp.status_code == 204:
                time.sleep(5)
                continue
            elif poll_resp.status_code == 200:
                rj = poll_resp.json()
                if rj.get("result") and not rj.get("error"):
                    print("      [Publish] Confirmation SUCCESS!")
                    return rj["result"]
                elif rj.get("error"):
                    raise RuntimeError(f"SDK Error: {rj['error']}")
            time.sleep(5)

        raise TimeoutError(f"Publish polling timed out for query {query_id}")

    def create_playlist(self, channel_id: str, title: str, claim_ids: List[str], desc: str = "") -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "method": "collection_create",
            "params": {
                "name": sanitize_claim_name(f"playlist-{title}"),
                "title": title,
                "description": desc or f"Collection: {title}",
                "channel_id": channel_id,
                "claims": claim_ids,
                "bid": "0.001",
                "blocking": True,
                "tags": ["playlist", "movie scenes", "archive"],
            },
            "id": int(time.time()),
        }
        res = self.session.post(LBRY_PROXY_URL, json=payload, timeout=60).json()
        if "error" in res:
            raise RuntimeError(f"Playlist creation failed: {res['error']}")
        return res.get("result", {})


def load_batch_file(path: str) -> List[Dict[str, Any]]:
    """Load items from a JSON, CSV, or TXT file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File {path} not found")

    items = []
    if p.suffix.lower() == ".json":
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            items = data if isinstance(data, list) else data.get("videos", [])
    elif p.suffix.lower() == ".csv":
        import csv
        with open(p, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                items.append(dict(row))
    else:
        # Plain text list of URLs
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    items.append({"url": line})
    return items


def main():
    parser = argparse.ArgumentParser(description="Upload videos to Odysee directly from GitHub or Google Drive without local downloads.")
    parser.add_argument("--url", type=str, help="Single direct video URL (GitHub release, raw GitHub link, etc.)")
    parser.add_argument("--gdrive", type=str, help="Google Drive link or File ID")
    parser.add_argument("--batch-file", type=str, help="Path to JSON, CSV, or TXT file with remote URLs")
    parser.add_argument("--auth-token", type=str, default=os.environ.get("ODYSEE_AUTH_TOKEN", ""), help="Odysee auth_token")
    parser.add_argument("--channel-id", type=str, default="", help="Channel claim ID to upload under")
    parser.add_argument("--title", type=str, default="", help="Video title (if single URL)")
    parser.add_argument("--description", type=str, default="", help="Video description")
    parser.add_argument("--tags", type=str, default="archive,movie,clip", help="Comma-separated tags")
    parser.add_argument("--thumbnail-url", type=str, default="", help="Remote thumbnail image URL")
    parser.add_argument("--playlist", type=str, default="", help="Playlist title to add videos to")
    parser.add_argument("--mode", choices=["auto", "cloud", "stream"], default="auto",
                        help="Upload mode: 'cloud' (Odysee server downloads directly), 'stream' (pipe through RAM), 'auto'")
    parser.add_argument("--dry-run", action="store_true", help="Preview items without uploading")

    args = parser.parse_args()

    # Collect items to process
    queue: List[Dict[str, Any]] = []
    if args.url:
        queue.append({
            "url": args.url,
            "title": args.title or Path(urlparse(args.url).path).stem.replace("_", " ").title(),
            "description": args.description,
            "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
            "thumbnail_url": args.thumbnail_url,
            "is_gdrive": False,
        })
    elif args.gdrive:
        queue.append({
            "url": args.gdrive,
            "title": args.title or "Google Drive Video",
            "description": args.description,
            "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
            "thumbnail_url": args.thumbnail_url,
            "is_gdrive": True,
        })
    elif args.batch_file:
        raw_items = load_batch_file(args.batch_file)
        for it in raw_items:
            u = it.get("url") or it.get("gdrive") or ""
            is_gd = "drive.google.com" in u or bool(it.get("gdrive"))
            t = it.get("title") or Path(urlparse(u).path).stem.replace("_", " ").title() or "Video"
            tags = it.get("tags", ["movie", "archive"])
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",") if x.strip()]
            queue.append({
                "url": u,
                "title": t,
                "description": it.get("description", ""),
                "tags": tags,
                "thumbnail_url": it.get("thumbnail_url", ""),
                "is_gdrive": is_gd,
                "movie_group": it.get("movie_title") or it.get("movie", ""),
            })

    if not queue:
        print("Error: No video URLs provided. Use --url, --gdrive, or --batch-file.")
        sys.exit(1)

    print(f"\n=======================================================")
    print(f" Remote to Odysee Uploader (Zero-Disk-Usage Pipeline)")
    print(f" Items in queue: {len(queue)}")
    print(f" Mode:           {args.mode}")
    print(f"=======================================================\n")

    if args.dry_run:
        print("[DRY RUN] Items to be uploaded directly from cloud/remote:")
        for idx, item in enumerate(queue, 1):
            src_type = "Google Drive (RAM Streaming)" if item["is_gdrive"] else "Remote URL (Odysee Cloud Ingestion)"
            print(f"[{idx}/{len(queue)}] {item['title']}")
            print(f"  Source Type: {src_type}")
            print(f"  URL:         {item['url'][:80]}")
            print(f"  Tags:        {', '.join(item['tags'])}")
            print(f"  Disk Usage:  0 Bytes (None)\n")
        if args.playlist:
            print(f"Would create/update Playlist: '{args.playlist}' with {len(queue)} clips.")
        return

    auth_token = args.auth_token.strip()
    if not auth_token:
        auth_token = input("Enter your Odysee auth_token: ").strip()
        if not auth_token:
            sys.exit(1)

    uploader = RemoteOdyseeUploader(auth_token)
    channels = uploader.list_channels()
    if not channels:
        print("Error: No channels found for this Odysee account.")
        sys.exit(1)

    ch_id = args.channel_id or channels[0]["claim_id"]
    ch_name = next((c["name"] for c in channels if c["claim_id"] == ch_id), channels[0]["name"])
    print(f"[Channel] Using channel: @{ch_name} ({ch_id})\n")

    uploaded_claim_ids = []
    manifest_records = {}

    for idx, item in enumerate(queue, 1):
        print(f"[{idx}/{len(queue)}] Processing: {item['title']}")
        claim_name = sanitize_claim_name(item["title"])

        thumb_cdn_url = None
        if item.get("thumbnail_url"):
            thumb_cdn_url = uploader.upload_thumbnail_from_url(item["thumbnail_url"])

        claim_params = {
            "name": claim_name,
            "title": item["title"],
            "description": item["description"] or f"Uploaded via Remote Ingestion: {item['title']}",
            "bid": "0.001",
            "languages": ["en"],
            "tags": item["tags"][:5],
            "channel_id": ch_id,
            "blocking": True,
            "preview": False,
        }
        if thumb_cdn_url:
            claim_params["thumbnail_url"] = thumb_cdn_url

        try:
            # Determine upload method
            use_stream = args.mode == "stream" or (args.mode == "auto" and item["is_gdrive"])

            if use_stream:
                # In-memory streaming (Google Drive / authenticated)
                resp, filename, size = GoogleDriveStreamer.open_stream(item["url"])
                res = uploader.publish_via_in_memory_stream(resp, filename, size, claim_params)
            else:
                # Cloud-to-cloud native remote URL
                res = uploader.publish_via_remote_url(item["url"], claim_params)

            claim_id = res.get("claim_id") or res.get("outputs", [{}])[0].get("claim_id", "")
            perm_url = res.get("permanent_url", f"lbry://{claim_name}")
            web_url = f"https://odysee.com/{perm_url.replace('lbry://', '')}"

            print(f"      [Success] Published: {web_url}\n")
            if claim_id:
                uploaded_claim_ids.append(claim_id)

            manifest_records[item["url"]] = {
                "title": item["title"],
                "claim_id": claim_id,
                "odysee_url": web_url,
                "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            time.sleep(2)

        except Exception as e:
            print(f"      [ERROR] Failed to process {item['title']}: {e}\n")

    # Playlist creation
    if args.playlist and uploaded_claim_ids:
        print(f"[Playlist] Creating collection '{args.playlist}' with {len(uploaded_claim_ids)} clips...")
        try:
            pl_res = uploader.create_playlist(ch_id, args.playlist, uploaded_claim_ids)
            pl_claim = pl_res.get("claim_id") or pl_res.get("outputs", [{}])[0].get("claim_id", "")
            print(f"[Playlist] Created: https://odysee.com/$/playlist/{pl_claim}")
        except Exception as e:
            print(f"[Playlist ERROR] {e}")

    # Save manifest
    m_path = Path("remote_odysee_manifest.json")
    with open(m_path, "w", encoding="utf-8") as f:
        json.dump(manifest_records, f, indent=2)
    print(f"\nCompleted. Logged {len(manifest_records)} records to {m_path.name}")


if __name__ == "__main__":
    main()
