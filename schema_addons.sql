-- ==============================================================
-- Add-On Schema: Orphan Archive & Multi-Model AI Comparison
-- Paste into Supabase Dashboard -> SQL Editor -> New Query -> Run
-- ==============================================================

create table if not exists public.scenes_archive (
    like public.scenes including all,
    archived_at timestamp with time zone default timezone('utc'::text, now()) not null,
    archive_reason text default 'missing_in_r2'
);

alter table public.scenes_archive enable row level security;
create policy "Allow public read on scenes_archive" on public.scenes_archive for select using (true);
create policy "Allow insert on scenes_archive" on public.scenes_archive for insert with check (true);
create policy "Allow update on scenes_archive" on public.scenes_archive for update using (true);

create table if not exists public.scene_analyses (
    analysis_id text primary key,
    scene_id text references public.scenes(scene_id) on delete cascade,
    ai_provider text not null,
    ai_model text not null,
    prompt_style text default 'standard',
    temperature real default 0.3,
    transcription text,
    visual_description text,
    synopsis text,
    mood text,
    noir_tropes jsonb default '[]'::jsonb,
    tags jsonb default '[]'::jsonb,
    raw_response jsonb default '{}'::jsonb,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

create index if not exists idx_scene_analyses_scene_id on public.scene_analyses(scene_id);
create index if not exists idx_scene_analyses_model on public.scene_analyses(ai_model);

alter table public.scene_analyses enable row level security;
create policy "Allow public read on scene_analyses" on public.scene_analyses for select using (true);
create policy "Allow insert on scene_analyses" on public.scene_analyses for insert with check (true);
create policy "Allow update on scene_analyses" on public.scene_analyses for update using (true);
