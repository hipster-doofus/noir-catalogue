#!/usr/bin/env python3
"""
Odysee Bulk Video & Playlist Uploader
====================================
Automates batch uploading of video clips to Odysee using their Web API (Tus v4 + JSON-RPC proxy),
with built-in support for automatic playlist / collection creation.

Features:
- Subdirectory harvesting: Reads catalog.json or film_noir_scenes.db to extract rich cinema metadata
  (director, year, mood, noir tropes, dialogue transcripts, visual descriptions, licenses, and thumbnails).
- Root clips harvesting: Uses ffprobe & filename heuristics to extract titles, scene numbers, timestamps,
  resolutions, codecs, and generates thumbnails using ffmpeg.
- Thumbnail upload: Directly publishes thumbnails to Odysee's CDN (thumbs.odycdn.com).
- Tus Chunked Uploads: Streams large video files chunk by chunk with an interactive progress bar.
- Playlist (Collection) Support:
  * Automatically creates playlists grouped by movie (e.g. "Inner Sanctum (1948) - Scene Collection")
  * Creates an all-in-one master playlist or a custom named playlist.
  * Can add clips to an existing playlist by claim ID.
  * Can build playlists retrospectively from `odysee_upload_manifest.json` without re-uploading!
- Manifest Tracking: Maintains `odysee_upload_manifest.json` so interrupted runs can be resumed seamlessly.
- Dry Run mode: Validate metadata and playlist grouping without making network calls.
"""

import os
import sys
import re
import json
import time
import base64
import sqlite3
import argparse
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests

# ---------------------------------------------------------
# Odysee API Endpoints (from official web frontend config)
# ---------------------------------------------------------
LBRY_PROXY_URL = "https://api.na-backend.odysee.com/api/v1/proxy"
ODYSEE_V4_API = "https://api.na-backend.odysee.com/api/v1/asynqueries"
ODYSEE_V4_INIT_UPLOAD = f"{ODYSEE_V4_API}/uploads/"
ODYSEE_THUMBNAIL_CDN_UPLOAD = "https://thumbs.odycdn.com/upload"
CHUNK_SIZE_DEFAULT = 8 * 1024 * 1024  # 8 MB chunks


def sanitize_claim_name(name: str) -> str:
    """Sanitize title/filename to a valid LBRY/Odysee claim name (alphanumeric and dashes only)."""
    s = re.sub(r"[^a-zA-Z0-9\-]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = f"clip-{int(time.time())}"
    return s[:40]


class OdyseeClient:
    """Handles authentication, channel listing, tus chunked uploads, and claim publishing."""

    def __init__(self, auth_token: str):
        self.auth_token = auth_token.strip()
        self.session = requests.Session()
        self.session.headers.update({
            "X-Lbry-Auth-Token": self.auth_token,
            "User-Agent": "OdyseeBulkUploader/1.0",
        })

    def list_channels(self) -> List[Dict[str, Any]]:
        """Fetch all channels owned by the authenticated user."""
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
            raise RuntimeError(f"Odysee API Error: {data['error'].get('message', data['error'])}")
        items = data.get("result", {}).get("items", [])
        channels = []
        for ch in items:
            channels.append({
                "claim_id": ch.get("claim_id"),
                "name": ch.get("name"),
                "title": ch.get("value", {}).get("title") or ch.get("name"),
            })
        return channels

    def upload_thumbnail(self, image_path: Path) -> Optional[str]:
        """Upload a local image (jpg/png/webp) to Odysee thumbnail CDN."""
        if not image_path.exists():
            return None
        print(f"      [Thumb] Uploading thumbnail '{image_path.name}' to Odysee CDN...")
        try:
            with open(image_path, "rb") as f:
                files = {"file-input": (image_path.name, f, "image/jpeg")}
                data = {"upload": "Upload"}
                res = requests.post(ODYSEE_THUMBNAIL_CDN_UPLOAD, files=files, data=data, timeout=60)
                res.raise_for_status()
                json_data = res.json()
                cdn_url = json_data.get("url") or json_data.get("message")
                if cdn_url and json_data.get("type") == "success":
                    print(f"      [Thumb] CDN URL: {cdn_url}")
                    return cdn_url
                print(f"      [Thumb] Warning: Unexpected CDN response: {json_data}")
        except Exception as e:
            print(f"      [Thumb] Error uploading thumbnail: {e}")
        return None

    def request_upload_token(self) -> Tuple[str, str]:
        """Request a Tus upload token and location endpoint from Odysee v4 API."""
        resp = self.session.post(ODYSEE_V4_INIT_UPLOAD, json={}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "upload_token_created" or not data.get("payload"):
            raise RuntimeError(f"Failed to obtain upload token: {data}")
        payload = data["payload"]
        return payload["token"], payload["location"]

    def upload_file_tus(self, file_path: Path, chunk_size: int = CHUNK_SIZE_DEFAULT) -> str:
        """Perform resumable Tus chunked upload of video file and return the completed Tus URL."""
        upload_token, upload_location = self.request_upload_token()
        file_size = file_path.stat().st_size
        filename = file_path.name

        # Encode metadata
        b64_filename = base64.b64encode(filename.encode()).decode()
        b64_filetype = base64.b64encode(b"video/mp4").decode()
        metadata_header = f"filename {b64_filename},filetype {b64_filetype}"

        # 1. Initialize Tus session (POST)
        init_headers = {
            "Authorization": f"Bearer {upload_token}",
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
            "Upload-Metadata": metadata_header,
        }
        res = requests.post(upload_location, headers=init_headers, timeout=60)
        if res.status_code not in (201, 200):
            raise RuntimeError(f"Tus initialization failed ({res.status_code}): {res.text}")

        upload_url = res.headers.get("Location")
        if not upload_url:
            raise RuntimeError("Tus server did not return a Location header")

        if not upload_url.startswith("http"):
            from urllib.parse import urljoin
            upload_url = urljoin(upload_location, upload_url)

        # 2. Upload file in chunks (PATCH)
        offset = 0
        start_time = time.time()
        print(f"      [Tus] Uploading {filename} ({file_size / (1024 * 1024):.2f} MB)...")

        with open(file_path, "rb") as f:
            while offset < file_size:
                f.seek(offset)
                chunk = f.read(chunk_size)
                patch_headers = {
                    "Authorization": f"Bearer {upload_token}",
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                }

                for attempt in range(3):
                    try:
                        patch_res = requests.patch(upload_url, headers=patch_headers, data=chunk, timeout=120)
                        if patch_res.status_code in (204, 200):
                            new_offset = int(patch_res.headers.get("Upload-Offset", offset + len(chunk)))
                            offset = new_offset
                            elapsed = time.time() - start_time
                            speed_mb = (offset / (1024 * 1024)) / (elapsed + 0.001)
                            pct = (offset / file_size) * 100
                            sys.stdout.write(f"\r      [Tus] Progress: {pct:5.1f}% [{offset // (1024*1024):3d}MB/{file_size // (1024*1024):3d}MB] at {speed_mb:4.2f} MB/s")
                            sys.stdout.flush()
                            break
                        elif patch_res.status_code in (409, 423):
                            time.sleep(2)
                            head_res = requests.head(upload_url, headers={"Authorization": f"Bearer {upload_token}", "Tus-Resumable": "1.0.0"}, timeout=30)
                            if head_res.status_code == 200:
                                offset = int(head_res.headers.get("Upload-Offset", offset))
                                break
                        else:
                            time.sleep(2 ** attempt)
                    except Exception as e:
                        if attempt == 2:
                            raise RuntimeError(f"Chunk upload failed at offset {offset}: {e}")
                        time.sleep(2 ** attempt)

        print(f"\n      [Tus] Upload finished in {time.time() - start_time:.1f}s!")
        return upload_url

    def publish_stream(self, claim_params: Dict[str, Any]) -> Dict[str, Any]:
        """Submit the claim_create query and poll until the publish succeeds."""
        payload = {
            "jsonrpc": "2.0",
            "method": "stream_create",
            "params": claim_params,
            "id": int(time.time()),
        }
        resp = self.session.post(f"{ODYSEE_V4_API}/", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "query_created" or not data.get("payload"):
            raise RuntimeError(f"Claim creation failed: {data}")

        query_id = data["payload"]["query_id"]
        print(f"      [Publish] Claim query created (Query ID: {query_id}). Polling confirmation...")

        poll_url = f"{ODYSEE_V4_API}/{query_id}"
        deadline = time.time() + (15 * 60)
        retry_delay = 5

        while time.time() < deadline:
            poll_resp = self.session.get(poll_url, timeout=30)
            if poll_resp.status_code == 204:
                time.sleep(retry_delay)
                continue
            elif poll_resp.status_code == 200:
                result_json = poll_resp.json()
                if result_json.get("result") and not result_json.get("error"):
                    sdk_result = result_json["result"]
                    print("      [Publish] Confirmation SUCCESS!")
                    return sdk_result
                elif result_json.get("error"):
                    raise RuntimeError(f"Publish error from SDK: {result_json['error']}")
            elif poll_resp.status_code == 404:
                time.sleep(retry_delay)
                continue
            else:
                time.sleep(retry_delay)

        raise TimeoutError(f"Publish status polling timed out for query {query_id}")

    def create_playlist(
        self,
        channel_id: str,
        title: str,
        claim_ids: List[str],
        description: str = "",
        thumbnail_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
        bid: str = "0.001"
    ) -> Dict[str, Any]:
        """Create a public playlist (collection) on Odysee under the specified channel."""
        claim_name = sanitize_claim_name(f"playlist-{title}")
        params = {
            "name": claim_name,
            "title": title,
            "description": description or f"Playlist collection: {title}",
            "channel_id": channel_id,
            "claims": claim_ids,
            "bid": bid,
            "blocking": True,
            "tags": tags or ["playlist", "movie scenes", "cinema"],
        }
        if thumbnail_url:
            params["thumbnail_url"] = thumbnail_url

        payload = {
            "jsonrpc": "2.0",
            "method": "collection_create",
            "params": params,
            "id": int(time.time()),
        }
        resp = self.session.post(LBRY_PROXY_URL, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Playlist creation failed: {data['error'].get('message', data['error'])}")
        result = data.get("result", {})
        output = result.get("outputs", [{}])[0] if "outputs" in result else result
        return output

    def update_playlist(
        self,
        collection_claim_id: str,
        channel_id: str,
        claim_ids: List[str],
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Update an existing playlist (collection) with new or appended claim IDs."""
        params: Dict[str, Any] = {
            "claim_id": collection_claim_id,
            "channel_id": channel_id,
            "claims": claim_ids,
            "replace": True,
            "clear_claims": True,
            "blocking": True,
        }
        if title:
            params["title"] = title
        if description:
            params["description"] = description
        if thumbnail_url:
            params["thumbnail_url"] = thumbnail_url
        if tags:
            params["tags"] = tags

        payload = {
            "jsonrpc": "2.0",
            "method": "collection_update",
            "params": params,
            "id": int(time.time()),
        }
        resp = self.session.post(LBRY_PROXY_URL, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Playlist update failed: {data['error'].get('message', data['error'])}")
        return data.get("result", {})

    def list_playlists(self, channel_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List existing playlists (collections) owned by the user."""
        params: Dict[str, Any] = {"page": 1, "page_size": 50}
        if channel_id:
            params["channel_id"] = channel_id
        payload = {
            "jsonrpc": "2.0",
            "method": "collection_list",
            "params": params,
            "id": int(time.time()),
        }
        try:
            resp = self.session.post(LBRY_PROXY_URL, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                return []
            items = data.get("result", {}).get("items", [])
            playlists = []
            for it in items:
                val = it.get("value", {})
                playlists.append({
                    "claim_id": it.get("claim_id"),
                    "name": it.get("name"),
                    "title": val.get("title") or it.get("name"),
                    "claims": val.get("claims", []),
                })
            return playlists
        except Exception:
            return []


# ---------------------------------------------------------
# Metadata Harvesting Functions
# ---------------------------------------------------------

def probe_video_metadata(video_path: Path) -> Dict[str, Any]:
    """Extract resolution, duration, codec, bitrate using ffprobe."""
    meta = {
        "duration": 0.0,
        "width": 1920,
        "height": 1080,
        "codec": "h264",
        "bitrate_kbps": 0,
        "size_bytes": video_path.stat().st_size,
    }
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,duration",
        "-show_entries", "format=duration,size,bit_rate",
        "-of", "json",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        stream = data.get("streams", [{}])[0] if data.get("streams") else {}
        fmt = data.get("format", {})

        meta["width"] = int(stream.get("width") or 1920)
        meta["height"] = int(stream.get("height") or 1080)
        meta["codec"] = stream.get("codec_name") or "h264"

        dur = stream.get("duration") or fmt.get("duration")
        if dur:
            meta["duration"] = float(dur)

        br = fmt.get("bit_rate")
        if br:
            meta["bitrate_kbps"] = int(float(br) / 1000)
    except Exception:
        meta["duration"] = 30.0
    return meta


def extract_thumbnail_frame(video_path: Path, output_thumb_path: Path, timestamp: float = 1.0) -> bool:
    """Extract a keyframe image from the video file using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(max(0.5, timestamp)),
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_thumb_path)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return output_thumb_path.exists()
    except Exception:
        return False


def harvest_subdirectory_catalog(subdir_path: Path) -> List[Dict[str, Any]]:
    """Load rich metadata from catalog.json or film_noir_scenes.db in the subdirectory."""
    items = []
    catalog_json_path = subdir_path / "catalog.json"
    sqlite_db_path = subdir_path / "film_noir_scenes.db"

    movies_map = {}
    scenes_list = []

    # Priority 1: catalog.json
    if catalog_json_path.exists():
        try:
            with open(catalog_json_path, "r", encoding="utf-8") as f:
                cat = json.load(f)
            for m in cat.get("movies", []):
                movies_map[m.get("archive_id")] = m
            scenes_list = cat.get("scenes", [])
            print(f"[Harvester] Loaded catalog.json with {len(movies_map)} films and {len(scenes_list)} scene records.")
        except Exception as e:
            print(f"[Harvester] Error reading catalog.json: {e}")

    # Priority 2: sqlite database fallback
    if not scenes_list and sqlite_db_path.exists():
        try:
            conn = sqlite3.connect(sqlite_db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            for row in c.execute("SELECT * FROM movies"):
                movies_map[row["movie_id"]] = dict(row)
            for row in c.execute("SELECT * FROM scenes"):
                scenes_list.append(dict(row))
            conn.close()
            print(f"[Harvester] Loaded film_noir_scenes.db with {len(movies_map)} films and {len(scenes_list)} scenes.")
        except Exception as e:
            print(f"[Harvester] Error reading sqlite db: {e}")

    # Build queue entries
    for sc in scenes_list:
        v_name = sc.get("video_filename")
        if not v_name:
            continue
        v_path = subdir_path / v_name
        if not v_path.exists():
            continue

        movie_id = sc.get("movie_id")
        movie = movies_map.get(movie_id, {})
        movie_title = movie.get("title") or movie_id.replace("_", " ").title()
        movie_year = movie.get("year") or movie.get("release_year") or ""
        director = movie.get("director") or "Unknown Director"
        license_str = movie.get("license") or "Public Domain"
        video_src_url = movie.get("video_url") or movie.get("source_archive_url") or ""

        scene_num = sc.get("scene_number", 1)
        mood = sc.get("mood", "Film Noir")
        tropes = sc.get("noir_tropes", [])
        if isinstance(tropes, str):
            try:
                tropes = json.loads(tropes)
            except Exception:
                tropes = [t.strip() for t in tropes.split(",") if t.strip()]

        tags_raw = sc.get("tags", [])
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except Exception:
                tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]

        start_tc = sc.get("start_timecode", "00:00:00")
        end_tc = sc.get("end_timecode", "00:00:00")
        dur_s = sc.get("duration_seconds") or 0.0
        transcript = sc.get("transcription") or ""
        visual_desc = sc.get("visual_description") or ""
        synopsis = sc.get("synopsis") or ""

        year_str = f" ({movie_year})" if movie_year else ""
        title = f"{movie_title}{year_str} - Scene {scene_num:02d} [{mood}]"
        claim_name = sanitize_claim_name(f"{movie_title}{year_str}-scene-{scene_num}")

        desc_lines = [
            f"**{synopsis}**" if synopsis else f"Film scene clip from {movie_title}.",
            "",
            f"🎬 **Film:** {movie_title}{year_str}",
            f"🎥 **Director:** {director}",
            f"⏱ **Scene {scene_num}:** {start_tc} - {end_tc} ({dur_s:.1f}s)",
            f"🎭 **Mood / Tone:** {mood}",
            f"🎞 **Noir Tropes:** {', '.join(tropes) if tropes else 'N/A'}",
        ]
        if transcript.strip():
            desc_lines.extend(["", "💬 **Dialogue / Transcript:**", f"> {transcript.strip()}"])
        if visual_desc.strip():
            desc_lines.extend(["", "👁 **Visual Framing:**", visual_desc.strip()])
        if movie.get("description"):
            desc_lines.extend(["", "📜 **Movie Summary:**", movie.get("description")])
        if license_str:
            desc_lines.extend(["", f"⚖ **License:** {license_str}"])
        if video_src_url:
            desc_lines.extend(["", f"🌐 **Source:** {video_src_url}"])

        description = "\n".join(desc_lines)

        final_tags = ["film noir", "cinema", "classic movie"]
        if mood:
            final_tags.append(mood.lower())
        for t in tags_raw:
            clean_t = re.sub(r"[^a-zA-Z0-9 ]+", "", t).strip().lower()
            if clean_t and clean_t not in final_tags:
                final_tags.append(clean_t)
        final_tags = final_tags[:5]

        thumb_file = sc.get("keyframe_filename")
        thumb_path = (subdir_path / thumb_file) if thumb_file else None
        if not thumb_path or not thumb_path.exists():
            candidate = subdir_path / f"{v_path.stem}_thumb.jpg"
            if candidate.exists():
                thumb_path = candidate

        items.append({
            "type": "subdir_rich",
            "file_path": v_path,
            "title": title,
            "movie_title": f"{movie_title}{year_str}".strip(),
            "scene_num": scene_num,
            "claim_name": claim_name,
            "description": description,
            "tags": final_tags,
            "thumb_path": thumb_path,
            "license": license_str,
            "duration": dur_s,
        })

    return items


def harvest_root_clips(clips_dir: Path) -> List[Dict[str, Any]]:
    """Harvest file metadata from clips in the root output_clips folder without json."""
    items = []
    root_mp4s = sorted(list(clips_dir.glob("*.mp4")))

    for v_path in root_mp4s:
        bn = v_path.stem

        m_scene = re.search(r"scene_?0*(\d+)", bn, re.IGNORECASE)
        scene_num = int(m_scene.group(1)) if m_scene else 1

        m_ts = re.search(r"_(\d+)s", bn)
        timestamp_s = m_ts.group(1) if m_ts else ""

        m_year = re.search(r"(19\d{2}|20\d{2})", bn)
        year = m_year.group(1) if m_year else ""

        clean = bn
        clean = re.sub(r"_scene_.*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"_(720p|1080p|BRrip|x264|BluRay|Remastered|Edition|movie|high_Q|avi|mp4).*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"_\d{3}_?$", "", clean)
        clean = clean.replace("__", ": ").replace("_", " ").strip()
        if year and clean.endswith(year):
            clean = clean[:-len(year)].strip()

        year_str = f" ({year})" if year else ""
        ts_str = f" [Offset {timestamp_s}s]" if timestamp_s else ""
        title = f"{clean}{year_str} - Scene {scene_num:02d}{ts_str}"
        claim_name = sanitize_claim_name(f"{clean}{year_str}-scene-{scene_num:02d}-{timestamp_s}")

        tech = probe_video_metadata(v_path)
        dur = tech["duration"]
        w = tech["width"]
        h = tech["height"]
        codec = tech["codec"].upper()
        br = tech["bitrate_kbps"]
        size_mb = tech["size_bytes"] / (1024 * 1024)

        desc_lines = [
            f"🎬 **Film Clip:** {clean}{year_str}",
            f"⏱ **Scene {scene_num:02d}**" + (f" (Timestamp: {timestamp_s}s)" if timestamp_s else ""),
            "",
            "📊 **Technical Video Specs:**",
            f"- **Resolution:** {w}x{h} ({'1080p' if h >= 1000 else '720p' if h >= 700 else 'SD'})",
            f"- **Duration:** {dur:.2f} seconds",
            f"- **Codec:** {codec}",
            f"- **Bitrate:** {br} kbps" if br else f"- **File Size:** {size_mb:.2f} MB",
            f"- **Source File:** `{v_path.name}`",
            "",
            "⚖ **Category:** Film & Animation / Movie Archive",
        ]
        description = "\n".join(desc_lines)

        tags = ["movie clip", "scene", "cinema", "film archive"]
        movie_tag = re.sub(r"[^a-zA-Z0-9 ]+", "", clean).strip().lower()
        if movie_tag and movie_tag not in tags:
            tags.insert(0, movie_tag[:20])
        tags = tags[:5]

        items.append({
            "type": "root_harvested",
            "file_path": v_path,
            "title": title,
            "movie_title": f"{clean}{year_str}".strip(),
            "scene_num": scene_num,
            "claim_name": claim_name,
            "description": description,
            "tags": tags,
            "thumb_path": None,
            "license": "Copyright / Fair Use Archive",
            "duration": dur,
        })

    return items


# ---------------------------------------------------------
# Playlist Builder Functions
# ---------------------------------------------------------

def build_playlists(
    client: Optional[OdyseeClient],
    channel_id: str,
    manifest: Dict[str, Any],
    playlist_mode: str,
    playlist_name: Optional[str] = None,
    playlist_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Group uploaded clips from manifest into playlists and create/update them on Odysee."""
    # Filter out internal metadata keys from manifest
    uploads = [v for k, v in manifest.items() if not k.startswith("_") and isinstance(v, dict) and v.get("claim_id")]

    if not uploads:
        print("[Playlists] No uploaded clips found with valid claim IDs to add to playlists.")
        return {}

    print(f"\n=======================================================")
    print(f" Processing Playlists (Mode: {playlist_mode})")
    print(f" Total eligible clips: {len(uploads)}")
    print(f"=======================================================\n")

    groups: Dict[str, List[Dict[str, Any]]] = {}

    if playlist_id:
        groups["existing_playlist"] = uploads
    elif playlist_mode == "all-in-one" or playlist_name:
        p_name = playlist_name or "Complete Movie Scenes Collection"
        groups[p_name] = uploads
    else:
        # Default: "by-movie"
        for item in uploads:
            m_title = item.get("movie_title")
            if not m_title:
                # Fallback extract from title
                m_title = item["title"].split(" - Scene")[0].strip()
            groups.setdefault(m_title, []).append(item)

    created_playlists = manifest.get("_playlists", {})

    for grp_name, clips in groups.items():
        claim_ids = [c["claim_id"] for c in clips]
        first_thumb = next((c.get("thumbnail_url") for c in clips if c.get("thumbnail_url")), None)

        if playlist_id:
            title = playlist_name or "Updated Playlist"
            print(f"[Playlist] Updating Playlist {playlist_id} with {len(claim_ids)} clips...")
            if dry_run:
                print(f"  [DRY RUN] Would update Playlist ID {playlist_id} with clips: {claim_ids[:3]}...")
                continue
            assert client is not None
            client.update_playlist(
                collection_claim_id=playlist_id,
                channel_id=channel_id,
                claim_ids=claim_ids,
                title=title if playlist_name else None
            )
            print(f"  [Playlist] Updated successfully: https://odysee.com/$/playlist/{playlist_id}")
            created_playlists[playlist_id] = {
                "title": title,
                "claim_id": playlist_id,
                "odysee_url": f"https://odysee.com/$/playlist/{playlist_id}",
                "clip_count": len(claim_ids),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            title = f"{grp_name} - Scene Collection" if playlist_mode == "by-movie" else grp_name
            desc = f"Collection of {len(clips)} scene clips from {grp_name}." if playlist_mode == "by-movie" else f"Collection of {len(clips)} movie scene clips."

            print(f"[Playlist] '{title}' ({len(claim_ids)} clips)")
            if dry_run:
                print(f"  [DRY RUN] Would create collection '{title}' under channel {channel_id}")
                print(f"  [DRY RUN] Included clips ({len(clips)}):")
                for c in clips:
                    print(f"    - {c.get('title')} (claim: {c.get('claim_id')})")
                continue

            assert client is not None
            try:
                res = client.create_playlist(
                    channel_id=channel_id,
                    title=title,
                    claim_ids=claim_ids,
                    description=desc,
                    thumbnail_url=first_thumb,
                    tags=["movie scenes", "cinema", "playlist"],
                )
                pl_claim_id = res.get("claim_id") or res.get("outputs", [{}])[0].get("claim_id", "")
                perm_url = res.get("permanent_url", f"lbry://{sanitize_claim_name(title)}")
                pl_url = f"https://odysee.com/{perm_url.replace('lbry://', '')}"
                print(f"  [Playlist] SUCCESS: {pl_url}")

                created_playlists[title] = {
                    "title": title,
                    "claim_id": pl_claim_id,
                    "permanent_url": perm_url,
                    "odysee_url": pl_url,
                    "clip_count": len(claim_ids),
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                time.sleep(2)
            except Exception as e:
                print(f"  [Playlist ERROR] Failed to create playlist '{title}': {e}")

    return created_playlists


# ---------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bulk upload local video clips and create playlists on Odysee.")
    parser.add_argument("--clips-dir", type=str, default=r"C:\Users\me\.gemini\antigravity\scratch\github_supabase_version\output_clips", help="Path to clips folder")
    parser.add_argument("--auth-token", type=str, default=os.environ.get("ODYSEE_AUTH_TOKEN", ""), help="Odysee session auth_token (or set ODYSEE_AUTH_TOKEN)")
    parser.add_argument("--channel-id", type=str, default="", help="Specific channel claim ID to upload to")
    parser.add_argument("--bid", type=str, default="0.001", help="LBC deposit bid per claim (default 0.001)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of videos to upload (0 for all)")
    parser.add_argument("--dry-run", action="store_true", help="Scan and display all metadata & playlists without uploading")
    parser.add_argument("--skip-root", action="store_true", help="Skip root folder clips (upload subdir only)")
    parser.add_argument("--skip-subdir", action="store_true", help="Skip subdir clips (upload root only)")

    # Playlist arguments
    parser.add_argument("--playlist", nargs="?", const="by-movie", default=None,
                        help="Enable playlist creation. Values: 'by-movie' (default), 'all-in-one', or custom playlist title")
    parser.add_argument("--playlist-name", type=str, default="", help="Custom title for the playlist")
    parser.add_argument("--playlist-id", type=str, default="", help="Existing playlist claim ID to append uploaded clips to")
    parser.add_argument("--create-playlists-from-manifest", action="store_true",
                        help="Skip video uploading and build playlists directly from odysee_upload_manifest.json")

    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    if not clips_dir.exists():
        print(f"Error: Directory '{clips_dir}' does not exist.")
        sys.exit(1)

    manifest_file = clips_dir / "odysee_upload_manifest.json"
    manifest: Dict[str, Any] = {}
    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            uploaded_count = len([k for k in manifest if not k.startswith("_")])
            print(f"[Manifest] Loaded existing upload manifest with {uploaded_count} completed uploads.")
        except Exception as e:
            print(f"[Manifest] Warning: Failed to read manifest: {e}")

    # ----------------------------------------------------
    # RETROSPECTIVE PLAYLIST CREATION FROM MANIFEST
    # ----------------------------------------------------
    if args.create_playlists_from_manifest:
        if not manifest:
            print("Error: No existing upload manifest found to build playlists from.")
            sys.exit(1)

        p_mode = args.playlist or ("all-in-one" if args.playlist_name else "by-movie")

        if args.dry_run:
            build_playlists(None, "DRY_RUN_CHANNEL", manifest, p_mode, args.playlist_name, args.playlist_id, dry_run=True)
            return

        auth_token = args.auth_token.strip()
        if not auth_token:
            auth_token = input("Enter your Odysee auth_token to create playlists: ").strip()
            if not auth_token:
                sys.exit(1)

        client = OdyseeClient(auth_token)
        channels = client.list_channels()
        ch_id = args.channel_id or (channels[0]["claim_id"] if channels else "")
        if not ch_id:
            print("Error: No channels found.")
            sys.exit(1)

        playlists = build_playlists(client, ch_id, manifest, p_mode, args.playlist_name, args.playlist_id, dry_run=False)
        manifest["_playlists"] = playlists
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"\n[Manifest] Saved updated playlists to {manifest_file}")
        return

    # Discover and build queue
    upload_queue: List[Dict[str, Any]] = []

    # 1. Harvest subdirectories (e.g. Noir_Batch_...)
    if not args.skip_subdir:
        for sub in clips_dir.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                sub_items = harvest_subdirectory_catalog(sub)
                upload_queue.extend(sub_items)

    # 2. Harvest root clips
    if not args.skip_root:
        root_items = harvest_root_clips(clips_dir)
        upload_queue.extend(root_items)

    print(f"\n=======================================================")
    print(f" Total clips discovered in queue: {len(upload_queue)}")
    print(f" Already uploaded in manifest:   {len([k for k in upload_queue if str(k['file_path']) in manifest])}")
    print(f"=======================================================\n")

    # Filter out already uploaded files
    pending_queue = [item for item in upload_queue if str(item["file_path"]) not in manifest]

    if args.limit > 0:
        pending_queue = pending_queue[:args.limit]
        print(f"[Queue] Limit applied: will process {len(pending_queue)} clips.")

    # ----------------------------------------------------
    # DRY RUN MODE
    # ----------------------------------------------------
    if args.dry_run:
        print("[DRY RUN] Inspecting metadata for queued clips:\n")
        for i, item in enumerate(pending_queue[:5], 1):
            print(f"--- [{i}/{min(5, len(pending_queue))}] {item['file_path'].name} ({item['type']}) ---")
            print(f"  Title:       {item['title']}")
            print(f"  Movie Group: {item.get('movie_title')}")
            print(f"  Claim Name:  {item['claim_name']}")
            print(f"  Tags:        {', '.join(item['tags'])}")
            print(f"  License:     {item['license']}")
            print(f"  Thumbnail:   {item['thumb_path'] if item['thumb_path'] else '[Will auto-generate with ffmpeg]'}")
            print(f"  Description Preview:\n{'-'*40}")
            print(item['description'])
            print(f"{'-'*40}\n")
        if len(pending_queue) > 5:
            print(f"... and {len(pending_queue) - 5} more clips ready for publishing.")

        if args.playlist:
            # Simulate mock manifest to preview playlist grouping
            mock_manifest = {}
            for it in upload_queue:
                mock_manifest[str(it["file_path"])] = {
                    "claim_id": f"mock_claim_{sanitize_claim_name(it['title'])[:10]}",
                    "title": it["title"],
                    "movie_title": it.get("movie_title"),
                    "thumbnail_url": "https://thumbs.odycdn.com/mock.webp",
                }
            build_playlists(None, "MOCK_CHANNEL", mock_manifest, args.playlist, args.playlist_name, args.playlist_id, dry_run=True)

        print("\n[DRY RUN COMPLETE] To start uploading, run without --dry-run and provide your --auth-token.")
        return

    # ----------------------------------------------------
    # LIVE UPLOAD MODE
    # ----------------------------------------------------
    auth_token = args.auth_token.strip()
    if not auth_token:
        print("\n" + "="*70)
        print(" Odysee Authentication Token Required")
        print("="*70)
        print("To upload videos to your Odysee channel, obtain your session token:")
        print("  1. Open your browser and go to https://odysee.com/ (sign in).")
        print("  2. Press F12 (Developer Tools) -> Console tab.")
        print("  3. Type: document.cookie  (or look in Application -> Cookies -> 'auth_token').")
        print("  4. Copy the value of 'auth_token'.")
        print("="*70)
        auth_token = input("\nEnter your Odysee auth_token: ").strip()
        if not auth_token:
            print("No auth token provided. Aborting.")
            sys.exit(1)

    client = OdyseeClient(auth_token)

    channels = client.list_channels()
    if not channels:
        print("Error: No channels found for this Odysee account. Please create a channel on odysee.com first.")
        sys.exit(1)

    selected_channel_id = args.channel_id
    selected_channel_name = ""
    if not selected_channel_id:
        if len(channels) == 1:
            selected_channel_id = channels[0]["claim_id"]
            selected_channel_name = channels[0]["name"]
            print(f"[Channel] Auto-selected your channel: {selected_channel_name} ({selected_channel_id})")
        else:
            print("\nYour Odysee Channels:")
            for idx, ch in enumerate(channels, 1):
                print(f"  [{idx}] {ch['title']} (@{ch['name']}) - Claim ID: {ch['claim_id']}")
            choice = input(f"\nSelect channel (1-{len(channels)}) [default 1]: ").strip()
            sel_idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(channels) else 0
            selected_channel_id = channels[sel_idx]["claim_id"]
            selected_channel_name = channels[sel_idx]["name"]
            print(f"[Channel] Selected: {selected_channel_name} ({selected_channel_id})")
    else:
        print(f"[Channel] Using provided channel ID: {selected_channel_id}")

    temp_thumb_path = clips_dir / "_temp_gen_thumb.jpg"

    # Process uploads
    success_count = 0
    fail_count = 0

    for i, item in enumerate(pending_queue, 1):
        v_path: Path = item["file_path"]
        print(f"\n{'='*70}")
        print(f"[{i}/{len(pending_queue)}] Uploading: {v_path.name}")
        print(f"  Title: {item['title']}")
        print(f"{'='*70}")

        try:
            # 1. Handle Thumbnail
            thumb_url = None
            local_thumb = item.get("thumb_path")
            if local_thumb and local_thumb.exists():
                thumb_url = client.upload_thumbnail(local_thumb)
            else:
                print("      [Thumb] Extracting keyframe from video...")
                if extract_thumbnail_frame(v_path, temp_thumb_path, timestamp=1.5):
                    thumb_url = client.upload_thumbnail(temp_thumb_path)
                    if temp_thumb_path.exists():
                        try:
                            temp_thumb_path.unlink()
                        except Exception:
                            pass

            # 2. Upload video binary via Tus
            tus_url = client.upload_file_tus(v_path)

            # 3. Create claim on Odysee
            claim_params = {
                "name": item["claim_name"],
                "title": item["title"],
                "description": item["description"],
                "bid": args.bid,
                "languages": ["en"],
                "tags": item["tags"],
                "channel_id": selected_channel_id,
                "file_path": tus_url,
                "blocking": True,
                "preview": False,
            }
            if thumb_url:
                claim_params["thumbnail_url"] = thumb_url
            if item.get("license"):
                claim_params["license"] = item["license"]

            publish_result = client.publish_stream(claim_params)

            claim_id = publish_result.get("claim_id") or publish_result.get("outputs", [{}])[0].get("claim_id", "")
            perm_url = publish_result.get("permanent_url", f"lbry://{item['claim_name']}")
            odysee_web_url = f"https://odysee.com/{perm_url.replace('lbry://', '')}"

            manifest[str(v_path)] = {
                "title": item["title"],
                "movie_title": item.get("movie_title"),
                "claim_name": item["claim_name"],
                "claim_id": claim_id,
                "permanent_url": perm_url,
                "odysee_url": odysee_web_url,
                "thumbnail_url": thumb_url,
                "channel_id": selected_channel_id,
                "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            print(f"      [Success] Published: {odysee_web_url}")
            success_count += 1
            time.sleep(2)

        except KeyboardInterrupt:
            print("\nUpload process interrupted by user. Progress saved in manifest.")
            break
        except Exception as e:
            print(f"      [ERROR] Upload failed for {v_path.name}: {e}")
            fail_count += 1
            time.sleep(3)

    print("\n" + "="*70)
    print(f" Batch video uploads complete. Successful: {success_count}, Failed: {fail_count}")
    print("="*70)

    # ----------------------------------------------------
    # PLAYLIST CREATION (IF REQUESTED)
    # ----------------------------------------------------
    if args.playlist:
        p_mode = args.playlist
        playlists = build_playlists(
            client=client,
            channel_id=selected_channel_id,
            manifest=manifest,
            playlist_mode=p_mode,
            playlist_name=args.playlist_name,
            playlist_id=args.playlist_id,
            dry_run=False
        )
        manifest["_playlists"] = playlists
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"\n[Playlists] Manifest updated with created playlists.")


if __name__ == "__main__":
    main()
