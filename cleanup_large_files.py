import os
import sqlite3
import json

# Root path of the repo to scan recursively
REPO_ROOT = os.path.abspath(".")
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")
DB_PATH = os.path.join(OUTPUT_DIR, "film_noir_scenes.db")
CATALOG_PATH = os.path.join(OUTPUT_DIR, "catalog.json")

# Size threshold: 50 MB in bytes
SIZE_LIMIT_BYTES = 50 * 1024 * 1024

def cleanup_large_files():
    removed_files = []
    
    # 1. Walk through the ENTIRE repository tree to catch .mp4 files in any location
    for root, dirs, files in os.walk(REPO_ROOT):
        # Skip .git directory to avoid corrupting git internal objects
        if ".git" in dirs:
            dirs.remove(".git")
            
        for filename in files:
            if filename.endswith(".mp4"):
                filepath = os.path.join(root, filename)
                try:
                    file_size = os.path.getsize(filepath)
                    if file_size >= SIZE_LIMIT_BYTES:
                        print(f"File {filepath} exceeds 50MB limit ({file_size / (1024*1024):.2f} MB). Deleting...")
                        os.remove(filepath)
                        removed_files.append(filename)
                        
                        # Delete corresponding thumbnail if present in the same directory
                        base_name = filename.rsplit("_", 2)[0] if "_" in filename else filename.replace(".mp4", "")
                        possible_thumbs = [
                            filename.replace(".mp4", "_thumb.jpg"),
                            filename.replace("_1080p.mp4", "_thumb.jpg"),
                            f"{base_name}_thumb.jpg"
                        ]
                        for thumb in possible_thumbs:
                            thumb_path = os.path.join(root, thumb)
                            if os.path.exists(thumb_path):
                                os.remove(thumb_path)
                                print(f"  Deleted thumbnail: {thumb_path}")
                except Exception as e:
                    print(f"Error processing {filepath}: {e}")

    if not removed_files:
        print("No .mp4 files anywhere in repo exceeded 50MB. Clean finish.")
        return

    # 2. Clean records from SQLite database (film_noir_scenes.db) if present
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            for fn in removed_files:
                cursor.execute("DELETE FROM scenes WHERE video_filename = ? OR clip_filename = ?", (fn, fn))
                cursor.execute("DELETE FROM scenes WHERE video_filename LIKE ?", (f"%{fn}%",))
            conn.commit()
            print(f"Removed database entries for {len(removed_files)} file(s) from {DB_PATH}.")
            conn.close()
        except Exception as e:
            print(f"Error updating database: {e}")

    # 3. Clean catalog.json if present
    if os.path.exists(CATALOG_PATH):
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                catalog_data = json.load(f)
            
            if isinstance(catalog_data, list):
                initial_count = len(catalog_data)
                catalog_data = [
                    item for item in catalog_data 
                    if item.get("filename") not in removed_files and item.get("clip_filename") not in removed_files
                ]
                print(f"Cleaned catalog.json: removed {initial_count - len(catalog_data)} record(s).")
                with open(CATALOG_PATH, "w", encoding="utf-8") as f:
                    json.dump(catalog_data, f, indent=2)
        except Exception as e:
            print(f"Error updating catalog.json: {e}")

if __name__ == "__main__":
    cleanup_large_files()
