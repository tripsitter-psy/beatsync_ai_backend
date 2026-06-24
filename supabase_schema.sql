-- BeatSync AI: Supabase Database Schema

-- 1. Create a table for users
-- This links directly to Supabase's built-in auth.users table
create table public.users (
  id uuid references auth.users not null primary key,
  email text,
  credits_balance integer default 3 not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable Row Level Security (RLS) for users
alter table public.users enable row level security;
create policy "Users can view their own profile." on public.users for select using (auth.uid() = id);
create policy "Users can update their own profile." on public.users for update using (auth.uid() = id);

-- Function to handle new user signups via triggers
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.users (id, email, credits_balance)
  values (new.id, new.email, 3);
  return new;
end;
$$ language plpgsql security definer;

-- Trigger to automatically create a profile when a new user signs up
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- 2. Create a table for render jobs
create type job_status as enum ('pending', 'processing_audio', 'processing_video', 'compositing', 'completed', 'failed');

create table public.render_jobs (
  id uuid default gen_random_uuid() primary key,
  user_id uuid references public.users(id) not null,
  status job_status default 'pending' not null,
  input_video_url text not null,
  style_id text not null,
  song_id text not null,
  output_video_url text,
  progress integer default 0 not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable Row Level Security (RLS) for render_jobs
alter table public.render_jobs enable row level security;
create policy "Users can view their own jobs." on public.render_jobs for select using (auth.uid() = user_id);
create policy "Users can insert their own jobs." on public.render_jobs for insert with check (auth.uid() = user_id);
-- Allow the backend (service role) full access to update jobs
create policy "Service role can update jobs" on public.render_jobs for update using (true);
