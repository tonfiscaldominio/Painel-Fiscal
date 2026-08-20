create extension if not exists pgcrypto;

create table if not exists public.companies (
  id uuid primary key default gen_random_uuid(),
  razao_social text not null,
  cnpj text not null unique,
  cpf_responsavel_hash text,
  inscricao_estadual text,
  municipio text,
  regime text,
  sefaz_password_encrypted text,
  sefaz_password text,
  created_at timestamptz not null default now()
);

create table if not exists public.sefaz_documents (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete cascade,
  numero_nfe text,
  cnpj_emitente text,
  razao_social_emitente text,
  data_emissao timestamptz,
  data_autorizacao timestamptz,
  valor numeric(18,2),
  chave_acesso text not null,
  uf_emitente text,
  situacao text,
  tipo_operacao text,
  origem_arquivo_hash text,
  created_at timestamptz not null default now(),
  unique(company_id, chave_acesso, origem_arquivo_hash)
);

alter table public.companies add column if not exists sefaz_password_encrypted text;
alter table public.companies add column if not exists sefaz_password text;

create index if not exists idx_sefaz_documents_company on public.sefaz_documents(company_id);
create index if not exists idx_sefaz_documents_key on public.sefaz_documents(chave_acesso);
create index if not exists idx_sefaz_documents_emission on public.sefaz_documents(data_emissao);

create table if not exists public.certificates (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete cascade,
  tipo text not null,
  resultado text not null,
  numero text,
  validade date,
  evidencias_path text,
  created_at timestamptz not null default now()
);

create table if not exists public.audit_findings (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete cascade,
  regra text not null,
  severidade text not null,
  chave_acesso text,
  descricao text not null,
  valor numeric(18,2),
  status text not null default 'Aberto',
  observacao text,
  created_at timestamptz not null default now()
);

alter table public.companies enable row level security;
alter table public.sefaz_documents enable row level security;
alter table public.certificates enable row level security;
alter table public.audit_findings enable row level security;

-- Aplicação compartilhada: usuários autenticados podem consultar e registrar dados.
-- Em produção, restrinja as políticas por organização ou empresa conforme a governança adotada.
drop policy if exists authenticated_companies on public.companies;
create policy authenticated_companies on public.companies for all to authenticated using (true) with check (true);

drop policy if exists authenticated_sefaz_documents on public.sefaz_documents;
create policy authenticated_sefaz_documents on public.sefaz_documents for all to authenticated using (true) with check (true);

drop policy if exists authenticated_certificates on public.certificates;
create policy authenticated_certificates on public.certificates for all to authenticated using (true) with check (true);

drop policy if exists authenticated_audit_findings on public.audit_findings;
create policy authenticated_audit_findings on public.audit_findings for all to authenticated using (true) with check (true);
