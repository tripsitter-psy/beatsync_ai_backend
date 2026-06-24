-- BeatSync AI — migration to support credits, subscriptions, and the real job shapes.
-- Safe to run on the existing schema (additive). Run in the Supabase SQL editor.

-- 1. Users: subscription + free-trial window -------------------------------
alter table public.users
  add column if not exists subscription_tier text not null default 'free',  -- 'free' | 'creator' | 'pro'
  add column if not exists trial_ends_at timestamptz,                        -- free tier expires here
  add column if not exists subscription_renews_at timestamptz,              -- monthly credit reset
  add column if not exists credits_monthly integer not null default 0;      -- allowance to refill each cycle

-- New signups: 3 free clips and a 2-month free window.
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.users (id, email, credits_balance, subscription_tier, trial_ends_at)
  values (new.id, new.email, 3, 'free', timezone('utc', now()) + interval '2 months');
  return new;
end;
$$ language plpgsql security definer;

-- Atomically spend one credit. Returns true if a credit was available AND the
-- account is still active (paid, or free within its trial window).
create or replace function public.consume_credit(p_user_id uuid)
returns boolean as $$
declare ok boolean;
begin
  update public.users
    set credits_balance = credits_balance - 1
    where id = p_user_id
      and credits_balance > 0
      and (subscription_tier <> 'free' or trial_ends_at is null or trial_ends_at > timezone('utc', now()))
    returning true into ok;
  return coalesce(ok, false);
end;
$$ language plpgsql security definer;

-- Give a credit back if a generation fails before doing billable work.
create or replace function public.refund_credit(p_user_id uuid)
returns void as $$
begin
  update public.users set credits_balance = credits_balance + 1 where id = p_user_id;
end;
$$ language plpgsql security definer;

-- 2. Render jobs: the real shapes (generate + beat-sync montages) ----------
-- status uses many pipeline values (preparing, rendering_video, interpolating,
-- compositing, upscaling, ...), so store it as text instead of the old enum.
alter table public.render_jobs
  alter column status type text using status::text,
  alter column status set default 'pending';

-- generate jobs have no song; beat-sync jobs have no style/single input video.
alter table public.render_jobs
  alter column style_id drop not null,
  alter column song_id drop not null,
  alter column input_video_url drop not null;

alter table public.render_jobs
  add column if not exists type text,                 -- 'generate_character' | 'beat_sync'
  add column if not exists props text,
  add column if not exists clip_urls jsonb,           -- beat-sync clip list
  add column if not exists start_sec double precision,
  add column if not exists error text;

-- Backend (service role) needs to insert jobs on the user's behalf too.
drop policy if exists "Service role can insert jobs" on public.render_jobs;
create policy "Service role can insert jobs" on public.render_jobs for insert with check (true);

-- 3. Security: Protect user billing fields from client-side updates -----------
create or replace function public.protect_user_billing_fields()
returns trigger as $$
begin
  -- Supabase client requests execute as either 'authenticated' or 'anon'.
  -- Transactions executed by the service-role key or admin bypass this check.
  if current_setting('role', true) in ('authenticated', 'anon') then
    if NEW.credits_balance is distinct from OLD.credits_balance or
       NEW.subscription_tier is distinct from OLD.subscription_tier or
       NEW.trial_ends_at is distinct from OLD.trial_ends_at or
       NEW.subscription_renews_at is distinct from OLD.subscription_renews_at or
       NEW.credits_monthly is distinct from OLD.credits_monthly then
      raise exception 'Billing and subscription fields are read-only for clients. Updates must go through the backend.';
    end if;
  end if;
  return NEW;
end;
$$ language plpgsql;

drop trigger if exists protect_user_billing_fields_trigger on public.users;
create trigger protect_user_billing_fields_trigger
  before update on public.users
  for each row execute procedure public.protect_user_billing_fields();

-- 4. RevenueCat webhooks integration table for idempotency and audit logs --
create table if not exists public.revenuecat_events (
  id text primary key, -- RevenueCat event ID
  user_id uuid references public.users(id),
  event_type text not null,
  payload jsonb not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable RLS for revenuecat_events (fully private, service role only)
alter table public.revenuecat_events enable row level security;

