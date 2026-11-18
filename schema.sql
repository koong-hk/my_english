create table if not exists public.english_lessons (
 study_date date primary key,
 korean text not null,
 natural_expression text not null,
 pattern text not null,
 lesson jsonb not null,
 created_at timestamptz not null default now()
);
create table if not exists public.english_submissions (
 id text primary key,
 study_date date not null references public.english_lessons(study_date),
 draft text not null,
 feedback jsonb,
 created_at timestamptz not null default now()
);
create table if not exists public.english_jobs (
 key text primary key,
 status text not null check (status in ('processing','completed','failed')),
 updated_at timestamptz not null default now()
);
alter table public.english_lessons enable row level security;
alter table public.english_submissions enable row level security;
alter table public.english_jobs enable row level security;
-- Service credentials are used exclusively on the Streamlit server and scheduled runner.
create or replace function public.claim_english_job(job_key text)
returns boolean language plpgsql security invoker set search_path = public as $$
declare claimed text;
begin
 insert into public.english_jobs(key, status) values(job_key, 'processing')
 on conflict(key) do update set status='processing', updated_at=now()
 where english_jobs.status='failed'
 returning key into claimed;
 return claimed is not null;
end;
$$;
revoke all on function public.claim_english_job(text) from public, anon, authenticated;
grant execute on function public.claim_english_job(text) to service_role;

-- 가족 계정별 PIN 로그인 및 기존 기록의 아버지 계정 이전
create extension if not exists pgcrypto with schema extensions;
create table if not exists public.english_profiles (
 id text primary key check (id in ('father','son')),
 display_name text not null,
 pin_hash text,
 failed_attempts integer not null default 0,
 locked_until timestamptz
);
insert into public.english_profiles(id,display_name) values ('father','아버지'),('son','아들')
on conflict(id) do nothing;
alter table public.english_profiles enable row level security;
alter table public.english_submissions add column if not exists profile_id text;
update public.english_submissions set profile_id='father' where profile_id is null;
alter table public.english_submissions alter column profile_id set not null;
create index if not exists english_submission_owner on public.english_submissions(profile_id,study_date);

create or replace function public.set_english_pin(account_id text, new_pin text)
returns void language plpgsql security invoker set search_path=public,extensions as $$
begin
 if new_pin !~ '^[0-9]{4}$' then raise exception 'PIN must contain 4 digits'; end if;
 update public.english_profiles set pin_hash=crypt(new_pin,gen_salt('bf',10)),
 failed_attempts=0, locked_until=null where id=account_id;
 if not found then raise exception 'Unknown profile'; end if;
end; $$;
create or replace function public.verify_english_pin(account_id text, supplied_pin text)
returns boolean language plpgsql security invoker set search_path=public,extensions as $$
declare p public.english_profiles;
begin
 select * into p from public.english_profiles where id=account_id for update;
 if not found or p.pin_hash is null then return false; end if;
 if p.locked_until > now() then return false; end if;
 if supplied_pin ~ '^[0-9]{4}$' and crypt(supplied_pin,p.pin_hash)=p.pin_hash then
 update public.english_profiles set failed_attempts=0,locked_until=null where id=account_id;
 return true;
 end if;
 if p.locked_until is not null then p.failed_attempts:=0; end if;
 update public.english_profiles set failed_attempts=p.failed_attempts+1,
 locked_until=case when p.failed_attempts+1>=5 then now()+interval '15 minutes' else null end
 where id=account_id;
 return false;
end; $$;
revoke all on function public.set_english_pin(text,text) from public,anon,authenticated;
revoke all on function public.verify_english_pin(text,text) from public,anon,authenticated;
grant execute on function public.set_english_pin(text,text) to service_role;
grant execute on function public.verify_english_pin(text,text) to service_role;
