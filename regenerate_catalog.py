import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CATALOG_PATH = BASE_DIR / "catalog.json"

catalog = {"movies": [], "scenes": []}
seen_scenes = set()

# Scan output directory for all MP4 clips
for mp4_file in sorted(OUTPUT_DIR.glob("*.mp4")):
    base_name = mp4_file.stem
    thumb_name = base_name + "_thumb.jpg"
    movie_id = base_name.split("_scene_")[0] if "_scene_" in base_name else base_name
    
    if base_name in seen_scenes:
        continue
    seen_scenes.add(base_name)

    scene_record = {
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
        "video_filename": mp4_file.name,
        "keyframe_filename": thumb_name if (OUTPUT_DIR / thumb_name).exists() else ""
    }
    catalog["scenes"].append(scene_record)

catalog["total_scenes"] = len(catalog["scenes"])

with open(CATALOG_PATH, "w", encoding="utf-8") as f:
    json.dump(catalog, f, indent=2)

# Also mirror to output/catalog.json
out_cat = OUTPUT_DIR / "catalog.json"
if OUTPUT_DIR.exists():
    with open(out_cat, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)

print(f"✨ Successfully regenerated catalog.json with {len(catalog['scenes'])} total scenes!")
