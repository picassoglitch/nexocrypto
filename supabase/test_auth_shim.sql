-- test_auth_shim.sql — minimal Supabase-compat shim for local Postgres and CI.
-- Do NOT apply this on a real Supabase project — those have the genuine `auth` schema.
-- Tests bootstrap it via the conftest fixture before applying the 0001/0002 migrations.

create schema if not exists auth;

-- Mirror Supabase's auth.users (just the columns we reference).
create table if not exists auth.users (
  id          uuid primary key default gen_random_uuid(),
  email       text,
  created_at  timestamptz not null default now()
);

-- Supabase's auth.uid() reads the subject claim from the request JWT.
-- We expose the same shape using a GUC the tests set via SET LOCAL.
create or replace function auth.uid() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

create or replace function auth.role() returns text
language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.role', true), ''), 'anon')
$$;

-- pgcrypto for gen_random_uuid().
create extension if not exists pgcrypto;
