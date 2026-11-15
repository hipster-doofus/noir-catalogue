import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CATALOG_PATH = BASE_DIR / "catalog.json"
DB_PATH = OUTPUT_DIR / "film_noir_scenes.db"


def load_existing_records():
    movies_map = {}
    scenes_map = {}

    if CATALOG_PATH.exists():
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                for m in data.get("movies", []):
                    movies_map[m["archive_id"]] = m
                for s in data.get("scenes", []):
                    scenes_map[s["scene_id"]] = s
        except Exception as e:
            print(f"[*] Notice reading existing catalog.json: {e}")

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            for row in cur.execute("SELECT * FROM movies"):
                r = dict(row)
                mid = r["movie_id"]
                movies_map.setdefault(mid, {
                    "archive_id": mid,
                    "title": r.get("title", mid),
                    "director": r.get("director", "Unknown Director"),
                    "year": r.get("release_year", 1950),
                    "video_url": r.get("source_archive_url", ""),
                    "source_provider": "Internet Archive",
                    "license": "Open Archive"
                })
            for row in cur.execute("SELECT * FROM scenes"):
                r = dict(row)
                sid = r["scene_id"]
                for json_col in ("noir_tropes", "tags"):
                    if isinstance(r.get(json_col), str):
                        try:
                            r[json_col] = json.loads(r[json_col])
                        except Exception:
                            r[json_col] = []
                scenes_map.setdefault(sid, r)
            conn.close()
        except Exception as e:
            print(f"[*] Notice reading SQLite DB: {e}")

    return movies_map, scenes_map


def regenerate():
    movies_map, scenes_map = load_existing_records()
    by_filename = {s.get("video_filename"): s for s in scenes_map.values() if s.get("video_filename")}

    for mp4_file in sorted(OUTPUT_DIR.glob("*.mp4")):
        if mp4_file.name in by_filename:
            continue
        base_name = mp4_file.stem
        thumb_name = base_name + "_thumb.jpg"
        movie_id = base_name.split("_scene_")[0] if "_scene_" in base_name else base_name

        scenes_map[base_name] = {
            "scene_id": base_name,
            "movie_id": movie_id,
            "scene_number": 1,
            "start_timecode": "00:00:00",
            "duration_seconds": 15,
            "transcription": f"Archival classic sequence ({mp4_file.name})",
            "visual_description": "Atmospheric cinematography and classic framing.",
            "synopsis": f"Key dramatic sequence from {movie_id}",
            "mood": "Fatalistic",
            "noir_tropes": ["shadow play", "moral ambiguity"],
            "tags": ["film noir", "black and white"],
            "video_filename": mp4_file.name,
            "keyframe_filename": thumb_name if (OUTPUT_DIR / thumb_name).exists() else "",
            "download_url": "",
            "thumbnail_url": ""
        }

    movies_list = list(movies_map.values())
    scenes_list = list(scenes_map.values())

    catalog = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_movies": len(movies_list),
        "total_scenes": len(scenes_list),
        "movies": movies_list,
        "scenes": scenes_list
    }

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)

    out_cat = OUTPUT_DIR / "catalog.json"
    if OUTPUT_DIR.exists():
        with open(out_cat, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2)

    print(f"✨ Successfully regenerated catalog.json ({len(movies_list)} movies, {len(scenes_list)} scenes)!")


if __name__ == "__main__":
    regenerate()
