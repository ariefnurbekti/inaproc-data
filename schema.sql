create table if not exists public.inaproc_rup (
    kode_rup text primary key check (length(kode_rup) > 0),
    data jsonb not null,
    source_url text not null,
    scraped_at timestamptz not null default now()
);

alter table public.inaproc_rup enable row level security;

create table if not exists public.inaproc_realisasi (
    kode_paket text primary key check (length(kode_paket) > 0),
    kode_rup text,
    data jsonb not null,
    source_url text not null,
    scraped_at timestamptz not null default now()
);

create index if not exists inaproc_realisasi_kode_rup_idx
    on public.inaproc_realisasi (kode_rup);

alter table public.inaproc_realisasi enable row level security;
