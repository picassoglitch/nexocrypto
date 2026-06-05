-- NexoCrypto — Supabase/Postgres schema (first pass)
-- Dedicated schema so it mounts cleanly inside the existing nexo-ai.world Supabase project.
-- RLS must be enabled on EVERY table; policies keyed to auth.uid() (and org_id where used).
-- Claude Code: flesh out columns/constraints/indexes as phases require. Keep secrets encrypted.

create schema if not exists nexocrypto;
set search_path = nexocrypto, public;

-- ── identity (reuse nexo-ai.world auth.users; this is the per-app profile/link) ──
create table users (
  id            uuid primary key references auth.users(id) on delete cascade,
  org_id        uuid,                         -- ties into nexo-ai.world org/RBAC model
  display_name  text,
  created_at    timestamptz not null default now()
);

-- ── connections (secrets are app-level encrypted blobs; never plaintext) ──
create table exchange_connections (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  exchange      text not null check (exchange in ('binance','lbank','bitunix')),
  api_key_enc   bytea not null,               -- envelope-encrypted, trade-only, no withdrawal
  api_secret_enc bytea not null,
  ip_allowlist  text[],
  status        text not null default 'untested',
  last_tested_at timestamptz,
  created_at    timestamptz not null default now()
);

create table telegram_channels (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  tg_channel_id text not null,
  title         text,
  enabled       boolean not null default true,
  score         numeric default 0,            -- updated from realized results
  created_at    timestamptz not null default now()
);

-- ── fees (per exchange/symbol/VIP, updatable from config) ──
create table fee_schedules (
  id            uuid primary key default gen_random_uuid(),
  exchange      text not null,
  symbol        text,                          -- null = default for exchange
  vip_level     text default 'regular',
  maker_bps     numeric not null,              -- e.g. 2.0 = 0.02%
  taker_bps     numeric not null,              -- e.g. 5.0 = 0.05%
  effective_at  timestamptz not null default now(),
  source        text
);

-- ── signals ──
create table parsed_signals (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  source        text not null,                 -- 'telegram'|'scanner'|'manual'
  channel_id    uuid references telegram_channels(id),
  pair          text not null,
  side          text not null check (side in ('long','short')),
  entry         numeric, stop_loss numeric, take_profits numeric[],
  leverage      numeric, timeframe text, margin_type text,
  raw_text      text,
  dedup_hash    text not null,
  created_at    timestamptz not null default now()
);

create table validated_signals (
  id            uuid primary key default gen_random_uuid(),
  parsed_id     uuid references parsed_signals(id) on delete cascade,
  user_id       uuid not null references users(id) on delete cascade,
  strategy      text,
  ev_net        numeric,                       -- expected value after all costs
  decision      text not null,                 -- 'approved'|'rejected'
  reject_reason text,                          -- always populated on reject
  snapshot_id   uuid,                          -- → market_snapshots
  created_at    timestamptz not null default now()
);

-- ── execution ──
create table trades (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  exchange      text, pair text, side text, strategy text,
  mode          text not null,                 -- backtest|paper|semi_auto|full_auto
  entry_price   numeric, exit_price numeric, qty numeric, leverage numeric,
  margin_type   text default 'isolated',
  fees_paid     numeric default 0, funding_paid numeric default 0,
  realized_pnl  numeric, status text,          -- open|closed|liquidated|cancelled
  continue_flag boolean default false,
  protected_floor numeric,                     -- ratcheting net floor (§ARCH 6)
  opened_at     timestamptz default now(), closed_at timestamptz
);

create table positions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  trade_id uuid references trades(id) on delete cascade,
  pair text, side text, qty numeric, entry_price numeric,
  liquidation_price numeric, unrealized_pnl numeric,
  updated_at timestamptz default now()
);

create table orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  trade_id uuid references trades(id) on delete cascade,
  exchange_order_id text, type text, side text, price numeric, qty numeric,
  reduce_only boolean default false, status text,
  idempotency_key text unique,                 -- duplicate-order prevention
  created_at timestamptz default now()
);

-- ── strategies / results / sims ──
create table strategies (
  id uuid primary key default gen_random_uuid(),
  key text unique not null, name text, params jsonb, enabled boolean default true
);
create table strategy_results (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  strategy text, mode text, sample_size int,
  win_rate numeric, profit_factor numeric, avg_rr numeric, max_drawdown numeric,
  fee_drag numeric, computed_at timestamptz default now()
);
create table backtests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  strategy text, pair text, timeframe text, period_start timestamptz, period_end timestamptz,
  params jsonb, metrics jsonb, optimistic boolean not null default true,  -- always labelled
  created_at timestamptz default now()
);
create table paper_trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  trade_id uuid references trades(id) on delete cascade, notes text
);

-- ── risk / mode gating ──
create table risk_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  name text, params jsonb not null,            -- all configurable rules live here
  is_default boolean default false
);
create table mode_state (
  user_id uuid primary key references users(id) on delete cascade,
  mode text not null default 'paper',
  paper_trades_count int default 0,
  paper_profit_factor numeric, paper_max_drawdown numeric,
  live_unlocked boolean not null default false, -- enforced gate
  account_protection_lock boolean not null default false,
  updated_at timestamptz default now()
);

-- ── llm / market context / ops ──
create table ai_evaluations (
  id uuid primary key default gen_random_uuid(),
  trade_id uuid references trades(id) on delete cascade,
  model text, kind text,                       -- thesis|continue_brief|daily_digest
  content text, created_at timestamptz default now()
);
create table market_snapshots (
  id uuid primary key default gen_random_uuid(),
  pair text, exchange text, payload jsonb,     -- klines/funding/oi/orderbook/spread/indicators
  taken_at timestamptz default now()
);
create table coinmarketcap_snapshots (
  id uuid primary key default gen_random_uuid(),
  payload jsonb,                               -- ranking/volume/trending/market-cap CONTEXT only
  taken_at timestamptz default now()
);
create table coinglass_snapshots (
  id uuid primary key default gen_random_uuid(),
  pair text, exchange text,
  payload jsonb,                               -- OI / aggregated funding / liquidation heatmap / long-short
  taken_at timestamptz default now()
);
create table notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  channel text, kind text, payload jsonb, status text, created_at timestamptz default now()
);
create table audit_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  actor text,                                  -- 'risk_engine'|'strategy'|'human'|'system'
  action text, reason text, details jsonb, created_at timestamptz default now()
);

-- RLS (apply to every table above):
--   alter table <t> enable row level security;
--   create policy <t>_owner on <t> using (user_id = auth.uid()) with check (user_id = auth.uid());
-- fee_schedules/strategies may be globally readable; writes restricted to service role / admins.
