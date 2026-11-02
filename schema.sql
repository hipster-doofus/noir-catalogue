-- ==============================================================
-- Supabase SQL Schema for Film Noir Scene Catalog
-- Paste this into your Supabase Dashboard: SQL Editor -> New Query -> Run
-- ==============================================================

-- 1. Movies Table
create table if not exists public.movies (
    movie_id text primary key,
    title text not null,
    director text,
    release_year integer,
    source_archive_url text,
    license text default 'Public Domain',
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 2. Scenes Table
create table if not exists public.scenes (
    scene_id text primary key,
    movie_id text references public.movies(movie_id) on delete cascade,
    scene_number integer not null,
    start_seconds real not null,
    end_seconds real not null,
    duration_seconds real not null,
    start_timecode text not null,
    end_timecode text not null,
    transcription text,
    visual_description text,
    synopsis text,
    mood text,
    noir_tropes jsonb default '[]'::jsonb,
    tags jsonb default '[]'::jsonb,
    keyframe_filename text,
    video_filename text,
    resolution text default '1920x1080',
    video_codec text default 'h264',
    audio_codec text default 'aac',
    download_url text,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 3. Row-Level Security (RLS) Configuration
alter table public.movies enable row level security;
alter table public.scenes enable row level security;

-- Read policy: Anyone with the public anon key can read movies and scenes
create policy "Allow public read access on movies" on public.movies for select using (true);
create policy "Allow public read access on scenes" on public.scenes for select using (true);

-- Insert/Update policy: Allow API key (anon or service_role) to insert & update
create policy "Allow insert on movies" on public.movies for insert with check (true);
create policy "Allow update on movies" on public.movies for update using (true);

create policy "Allow insert on scenes" on public.scenes for insert with check (true);
create policy "Allow update on scenes" on public.scenes for update using (true);

-- 4. Fast Query Indexes
create index if not exists idx_scenes_movie_id on public.scenes(movie_id);
create index if not exists idx_scenes_mood on public.scenes(mood);
create index if not exists idx_scenes_duration on public.scenes(duration_seconds);
