# 🎬 Noir Vault — Multi-Source Film Noir AI Extractor & Web Gallery

A completely cloud-hosted, zero-cost pipeline that extracts, indexes, and showcases classic film noir clips using **GitHub Actions**, **Supabase (PostgreSQL)**, and **GitHub Pages**.

---

## ✨ Key Capabilities

1. **Blended Multi-Source Catalog:**
   - **Internet Archive:** Rare original feature scans.
   - **Library of Congress:** National Film Registry archival preservation masters.
   - **YouTube Restorations:** Digital 1080p/4K restorations streamed seamlessly via `yt-dlp`.
2. **Batch Processing (Up to 10 Films):**
   - Extract up to 10 films in a single run (generating up to 30 pristine 1080p scenes).
   - Excludes already-indexed films from Supabase to guarantee fresh titles.
3. **AI Multimodal Perception:**
   - Spoken dialogue transcription.
   - Visual cinematography breakdown (lighting, chiaroscuro, staging).
   - Narrative synopsis & summary.
   - Dominant emotional mood (*Fatalistic, Melancholic, Tense, Paranoid*).
   - Searchable Film Noir tropes (*voiceover, femme fatale, smoke, rain-slicked streets*).
4. **Interactive Web Gallery (`index.html`):**
   - Hosted for free on **GitHub Pages**.
   - Real-time search across spoken quotes, titles, directors, and tropes.
   - Categorization by mood and source provider.
   - In-browser 1080p video player modal with synchronized transcripts.
   - Real-time sync with your Supabase database.

---

## 🌐 Enabling Your Free Web Gallery (GitHub Pages)

To have your live web gallery hosted automatically at `https://hipster-doofus.github.io/noir-catalogue/`:

1. In your GitHub repository, click **Settings**.
2. In the left sidebar, click **Pages**.
3. Under **Build and deployment ➔ Source**, select **GitHub Actions**.

*That's it! Every time the workflow runs, your website automatically updates with the latest clips and records.*

---

## 🚀 Running a Batch Run (Up to 10 Films)

1. Go to **[Actions ➔ "Extract Film Noir Scenes & Publish Web Vault"](https://github.com/hipster-doofus/noir-catalogue/actions)**.
2. Click **Run workflow**:
   - `Number of movies to process`: Set between `1` and `10` (default is `10`).
   - Click the green **Run workflow** button.

---

## 📊 Where to Find Results

* **Live Web Gallery:** `https://hipster-doofus.github.io/noir-catalogue/`
* **Direct Video Downloads:** [GitHub Releases](https://github.com/hipster-doofus/noir-catalogue/releases)
* **Live Database:** [Supabase Dashboard ➔ Table Editor](https://supabase.com)
