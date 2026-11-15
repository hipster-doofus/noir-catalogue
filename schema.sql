-- ==============================================================
-- Supabase SQL Schema for Noir Vault (with pgvector & Full-Text Search)
-- Paste into Supabase Dashboard -> SQL Editor -> New Query -> Run
-- ==============================================================

create extension if not exists vector;

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
    thumbnail_url text,
    clip_embedding vector(512),
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Ensure columns exist if upgrading an existing table
alter table public.scenes add column if not exists thumbnail_url text;
alter table public.scenes add column if not exists clip_embedding vector(512);

-- Weighted Full-Text Search Column
alter table public.scenes
  add column if not exists search_tsv tsvector
  generated always as (
    setweight(to_tsvector('english', coalesce(transcription, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(synopsis, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(visual_description, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(mood, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(movie_id, '')), 'C')
  ) stored;

-- 3. Row-Level Security (RLS) Configuration
alter table public.movies enable row level security;
alter table public.scenes enable row level security;

drop policy if exists "Allow public read access on movies" on public.movies;
create policy "Allow public read access on movies" on public.movies for select using (true);

drop policy if exists "Allow public read access on scenes" on public.scenes;
create policy "Allow public read access on scenes" on public.scenes for select using (true);

drop policy if exists "Allow insert on movies" on public.movies;
create policy "Allow insert on movies" on public.movies for insert with check (true);

drop policy if exists "Allow update on movies" on public.movies;
create policy "Allow update on movies" on public.movies for update using (true);

drop policy if exists "Allow insert on scenes" on public.scenes;
create policy "Allow insert on scenes" on public.scenes for insert with check (true);

drop policy if exists "Allow update on scenes" on public.scenes;
create policy "Allow update on scenes" on public.scenes for update using (true);

-- 4. Fast Query & Vector Indexes
create index if not exists idx_scenes_movie_id on public.scenes(movie_id);
create index if not exists idx_scenes_mood on public.scenes(mood);
create index if not exists idx_scenes_duration on public.scenes(duration_seconds);
create index if not exists idx_scenes_search_tsv on public.scenes using gin(search_tsv);
create index if not exists idx_scenes_clip_embedding on public.scenes using hnsw (clip_embedding vector_cosine_ops);

-- 5. Server-Side Full-Text + Mood Search RPC
create or replace function public.search_scenes(
  search_query text default '',
  mood_filter text default 'all',
  result_limit int default 60,
  result_offset int default 0
)
returns table (
  scene_id text,
  movie_id text,
  scene_number int,
  start_seconds real,
  end_seconds real,
  duration_seconds real,
  start_timecode text,
  end_timecode text,
  transcription text,
  visual_description text,
  synopsis text,
  mood text,
  noir_tropes jsonb,
  tags jsonb,
  keyframe_filename text,
  video_filename text,
  download_url text,
  thumbnail_url text,
  movie_title text,
  release_year int,
  director text,
  created_at timestamptz
)
language sql stable as $$
  select
    s.scene_id, s.movie_id, s.scene_number,
    s.start_seconds, s.end_seconds, s.duration_seconds,
    s.start_timecode, s.end_timecode,
    s.transcription, s.visual_description, s.synopsis, s.mood,
    s.noir_tropes, s.tags,
    s.keyframe_filename, s.video_filename,
    s.download_url, s.thumbnail_url,
    m.title as movie_title,
    m.release_year,
    m.director,
    s.created_at
  from public.scenes s
  left join public.movies m on m.movie_id = s.movie_id
  where
    (mood_filter = 'all' or mood_filter = '' or s.mood ilike '%' || mood_filter || '%')
    and (
      search_query is null
      or trim(search_query) = ''
      or s.search_tsv @@ websearch_to_tsquery('english', search_query)
      or s.transcription ilike '%' || search_query || '%'
      or m.title ilike '%' || search_query || '%'
      or s.noir_tropes::text ilike '%' || search_query || '%'
    )
  order by s.created_at desc
  limit result_limit
  offset result_offset;
$$;
