---

### 5. `cleanup_large_files.py`
*Fixed to handle `catalog.json` as a dictionary (`{"movies": [...], "scenes": [...]}`) and keep both `output/catalog.json` and root `catalog.json` in sync.*

```python
import os
import sqlite3
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = REPO_ROOT / "output"
DB_PATH = OUTPUT_DIR / "film_noir_scenes.db"
CATALOG_PATHS = [OUTPUT_DIR / "catalog.json", REPO_ROOT / "catalog.json"]
SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB


def cleanup_large_files():
    removed_files = set()

    for root, dirs, files in os.walk(REPO_ROOT):
        if ".git" in dirs:
            dirs.remove(".git")

        for filename in files:
            if filename.endswith(".mp4"):
                filepath = Path(root) / filename
                try:
                    file_size = filepath.stat().st_size
                    if file_size >= SIZE_LIMIT_BYTES:
                        print(f"File {filepath} exceeds 50MB limit ({file_size / (1024*1024):.2f} MB). Deleting...")
                        filepath.unlink(missing_ok=True)
                        removed_files.add(filename)

                        base_name = filename.rsplit("_", 2)[0] if "_" in filename else filename.replace(".mp4", "")
                        possible_thumbs = [
                            filename.replace(".mp4", "_thumb.jpg"),
                            filename.replace("_1080p.mp4", "_thumb.jpg"),
                            f"{base_name}_thumb.jpg"
                        ]
                        for thumb in possible_thumbs:
                            thumb_path = Path(root) / thumb
                            if thumb_path.exists():
                                thumb_path.unlink(missing_ok=True)
                                print(f"  Deleted thumbnail: {thumb_path}")
                except Exception as e:
                    print(f"Error processing {filepath}: {e}")

    if not removed_files:
        print("No .mp4 files anywhere in repo exceeded 50MB. Clean finish.")
        return

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            for fn in removed_files:
                cursor.execute("DELETE FROM scenes WHERE video_filename = ?", (fn,))
            conn.commit()
            conn.close()
            print(f"Removed database entries for {len(removed_files)} file(s) from {DB_PATH}.")
        except Exception as e:
            print(f"Error updating database: {e}")

    for cat_path in CATALOG_PATHS:
        if cat_path.exists():
            try:
                with open(cat_path, "r", encoding="utf-8") as f:
                    catalog_data = json.load(f)

                if isinstance(catalog_data, dict) and "scenes" in catalog_data:
                    before = len(catalog_data["scenes"])
                    catalog_data["scenes"] = [
                        s for s in catalog_data["scenes"]
                        if s.get("video_filename") not in removed_files
                        and s.get("clip_filename") not in removed_files
                    ]
                    catalog_data["total_scenes"] = len(catalog_data["scenes"])
                    with open(cat_path, "w", encoding="utf-8") as f:
                        json.dump(catalog_data, f, indent=2)
                    print(f"Cleaned {cat_path.name}: removed {before - len(catalog_data['scenes'])} record(s).")
            except Exception as e:
                print(f"Error updating {cat_path}: {e}")


if __name__ == "__main__":
    cleanup_large_files()