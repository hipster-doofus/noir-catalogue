import os
import json
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

#GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "hipster-doofus/noir-catalogue")
GITHUB_REPO = os.environ.get("SOURCE_REPO", "hipster-doofus/noir-vault-v2")
HEADERS = {"User-Agent": "GitHub-Actions-Migrator"}

def migrate_release_assets():
    releases_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    print(f"🔎 Fetching releases for {GITHUB_REPO}...")
    
    resp = requests.get(releases_url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch releases: {resp.status_code}")
        return

    releases = resp.json()
    print(f"Found {len(releases)} release batches.")

    downloaded_count = 0
    for rel in releases:
        assets = rel.get("assets", [])
        print(f"\n📦 Processing Release: {rel.get('name', rel.get('tag_name'))} ({len(assets)} assets)")
        
        for asset in assets:
            name = asset.get("name")
            download_url = asset.get("browser_download_url")
            
            # Target MP4s, JPGs, and DB files
            if name.endswith((".mp4", ".jpg", ".db", ".json")):
                target_path = OUTPUT_DIR / name
                if target_path.exists():
                    print(f"  [⏭️ Skipped] {name} already exists in output/")
                    continue
                
                print(f"  [⬇️ Downloading] {name} -> output/")
                with requests.get(download_url, headers=HEADERS, stream=True) as r:
                    r.raise_for_status()
                    with open(target_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                downloaded_count += 1

    print(f"\n🎉 Migration Complete! Downloaded {downloaded_count} asset(s) directly into output/.")

if __name__ == "__main__":
    migrate_release_assets()
