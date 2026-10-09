-- Replica DB redesign. Apply to the existing baseline_chat_schema with apply_migration.
-- No user, table, or application row is deleted. Auth is managed by Supabase.
-- Server-only writes; authenticated reads are filtered by RLS.
create extension if not exists vector with schema extensions;
create schema if not exists replica_private;
revoke all on schema replica_private from public, anon, authenticated;
grant usage on schema replica_private to service_role;

alter table public.personas
  add column status text not null default 'active' check (status in ('active', 'deleting')),
  add column data_revision bigint not null default 0 check (data_revision >= 0),
  add column updated_at timestamptz not null default now(),
  add constraint personas_name_not_blank check (length(btrim(name)) > 0);
alter table public.chat_sessions
  add column title text,
  add column updated_at timestamptz not null default now();
alter table public.chat_turns
  add column turn_no bigint generated always as identity,
  add column request_hash text check (request_hash ~ '^[0-9a-f]{64}$'),
  add column audio_requested boolean not null default false,
  add column stt_attempt integer not null default 0 check (stt_attempt >= 0),
  add column llm_attempt integer not null default 0 check (llm_attempt >= 0),
  add column tts_attempt integer not null default 0 check (tts_attempt >= 0),
  add column stt_started_at timestamptz,
  add column llm_started_at timestamptz,
  add column tts_started_at timestamptz,
  add column updated_at timestamptz not null default now();
alter table public.chat_turns drop constraint chat_turns_tts_after_answer;
alter table public.chat_turns add constraint chat_turns_tts_after_completed_answer
  check (tts_status is null or answer_status is not distinct from 'done');
alter table public.chat_turns add constraint chat_turns_question_not_blank
  check (question_status <> 'done' or length(btrim(question)) > 0);
alter table public.chat_turns add constraint chat_turns_answer_not_blank
  check (answer_status is distinct from 'done' or length(btrim(answer)) > 0);
alter table public.chat_turns add constraint chat_turns_text_question_done
  check (input_mode <> 'text' or question_status = 'done');
create index chat_turns_session_user_order_idx
  on public.chat_turns (session_id, user_id, turn_no);
create index chat_turns_pending_llm_idx on public.chat_turns (llm_started_at)
  where answer_status = 'pending';
create index chat_turns_pending_stt_idx on public.chat_turns (stt_started_at)
  where question_status = 'pending';
create index chat_turns_pending_tts_idx on public.chat_turns (tts_started_at)
  where tts_status = 'pending';

create table public.records (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  persona_id uuid not null,
  kind text not null check (kind in ('text', 'audio')),
  original_name text not null check (length(btrim(original_name)) > 0),
  bucket_id text not null default 'replica-records' check (bucket_id = 'replica-records'),
  object_path text not null,
  mime_type text not null,
  size_bytes bigint check (size_bytes >= 0),
  source_description text,
  target_speaker text,
  status text not null default 'uploading'
    check (status in ('uploading', 'uploaded', 'processing', 'ready', 'failed', 'deleting')),
  consent_at timestamptz,
  consent_version text,
  consent_scopes text[] not null default array['personalization']::text[],
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id, persona_id),
  unique (bucket_id, object_path),
  foreign key (persona_id, user_id) references public.personas (id, user_id) on delete restrict,
  check (object_path = user_id::text || '/' || id::text || '/original'),
  check (consent_scopes <@ array['personalization', 'training']::text[]),
  check (status <> 'ready' or (consent_at is not null and
    consent_version is not null and length(btrim(consent_version)) > 0
    and 'personalization' = any(consent_scopes)))
);
create index records_persona_owner_idx on public.records (persona_id, user_id, created_at, id);
create index records_owner_idx on public.records (user_id);

create table public.record_text_versions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  persona_id uuid not null,
  record_id uuid not null,
  revision integer not null check (revision > 0),
  content text not null check (length(btrim(content)) > 0),
  status text not null default 'draft' check (status in ('draft', 'confirmed', 'superseded')),
  confirmed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id, persona_id),
  unique (record_id, revision),
  foreign key (record_id, user_id, persona_id)
    references public.records (id, user_id, persona_id) on delete cascade,
  check (status <> 'confirmed' or confirmed_at is not null)
);
create unique index record_text_one_confirmed_idx on public.record_text_versions (record_id)
  where status = 'confirmed';
create index record_text_record_owner_idx on public.record_text_versions (record_id, user_id, persona_id);
create index record_text_owner_idx on public.record_text_versions (user_id);

create table public.style_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  persona_id uuid not null,
  version integer not null check (version > 0),
  source_revision bigint not null check (source_revision >= 0),
  content jsonb not null check (jsonb_typeof(content) = 'object' and content <> '{}'::jsonb),
  model_name text not null check (length(btrim(model_name)) > 0),
  status text not null default 'ready' check (status in ('ready', 'stale')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id, persona_id),
  unique (persona_id, version),
  foreign key (persona_id, user_id) references public.personas (id, user_id) on delete cascade
);
create unique index style_profile_one_ready_idx on public.style_profiles (persona_id)
  where status = 'ready';
create index style_profiles_persona_owner_idx on public.style_profiles (persona_id, user_id);
create index style_profiles_owner_idx on public.style_profiles (user_id);

create table public.profile_sources (
  profile_id uuid not null,
  text_version_id uuid not null,
  user_id uuid not null,
  persona_id uuid not null,
  primary key (profile_id, text_version_id),
  foreign key (profile_id, user_id, persona_id)
    references public.style_profiles (id, user_id, persona_id) on delete cascade,
  foreign key (text_version_id, user_id, persona_id)
    references public.record_text_versions (id, user_id, persona_id) on delete cascade
);
create index profile_sources_profile_owner_idx on public.profile_sources (profile_id, user_id, persona_id);
create index profile_sources_text_owner_idx on public.profile_sources (text_version_id, user_id, persona_id);
create index profile_sources_owner_idx on public.profile_sources (user_id);

create table public.record_chunks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  persona_id uuid not null,
  text_version_id uuid not null,
  chunk_no integer not null check (chunk_no >= 0),
  content text not null check (length(btrim(content)) > 0),
  embedding extensions.vector not null,
  embedding_model text not null check (length(btrim(embedding_model)) > 0),
  embedding_dimensions integer not null check (embedding_dimensions > 0),
  created_at timestamptz not null default now(),
  unique (text_version_id, embedding_model, chunk_no),
  foreign key (text_version_id, user_id, persona_id)
    references public.record_text_versions (id, user_id, persona_id) on delete cascade,
  check (extensions.vector_dims(embedding) = embedding_dimensions),
  check ((embedding operator(extensions.<#>) embedding) < 0)
);
create index record_chunks_text_owner_idx on public.record_chunks (text_version_id, user_id, persona_id);
create index record_chunks_search_scope_idx
  on public.record_chunks (user_id, persona_id, embedding_model, embedding_dimensions);
-- The model/dimension has not been chosen. Use filtered exact search for now.
-- Add an HNSW index in a new migration once one embedding model is agreed.

create table public.deletion_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users (id) on delete set null,
  target_type text not null check (target_type in ('record', 'session', 'persona', 'account')),
  target_id uuid not null,
  status text not null default 'pending' check (status in ('pending', 'running', 'failed', 'completed')),
  manifest jsonb not null default '{}'::jsonb check (jsonb_typeof(manifest) = 'object'),
  remaining_steps text[] not null default array['storage', 'database']::text[],
  attempt_count integer not null default 0 check (attempt_count >= 0),
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  check (remaining_steps <@ array['storage', 'database', 'auth', 'external_index', 'adapter']::text[]),
  check (status <> 'completed' or (cardinality(remaining_steps) = 0 and finished_at is not null))
);
create unique index deletion_jobs_one_open_target_idx
  on public.deletion_jobs (user_id, target_type, target_id) where status <> 'completed';
create index deletion_jobs_owner_idx on public.deletion_jobs (user_id);
create index deletion_jobs_work_idx on public.deletion_jobs (status, created_at)
  where status in ('pending', 'failed');

comment on table public.records is 'Private uploaded source metadata; object bytes live in Storage.';
comment on table public.record_text_versions is 'Reviewed text revisions. Confirmed content is immutable; one current version per record.';
comment on table public.style_profiles is 'Versioned tone profiles. Stale profiles must never enter a prompt.';
comment on table public.profile_sources is 'Exact confirmed text versions used to build a tone profile.';
comment on table public.record_chunks is 'RAG chunks and vectors. Only current confirmed versions can be searched.';
comment on table public.deletion_jobs is 'Durable deletion manifest; survives deletion of target rows. A worker must delete actual Storage files.';
comment on column public.chat_turns.turn_no is 'Stable database-assigned ordering; numbers may have gaps.';
comment on column public.chat_turns.tts_status is 'Actual TTS execution state; never set while LLM is still pending.';

create function replica_private.touch_updated_at() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin new.updated_at := clock_timestamp(); return new; end;
$$;
do $$
declare t text;
begin
  foreach t in array array['personas', 'chat_sessions', 'chat_turns', 'records',
    'record_text_versions', 'style_profiles', 'deletion_jobs'] loop
    execute format('create trigger touch_updated_at before update on public.%I
      for each row execute function replica_private.touch_updated_at()', t);
  end loop;
end;
$$;

-- Changes to approved inputs invalidate all previously published profiles for
-- this persona. RLS/search also exclude superseded versions and deleting records.
create function replica_private.invalidate_personalization() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare owner_id uuid; subject_id uuid; invalidate boolean := false;
begin
  if tg_op = 'DELETE' then owner_id := old.user_id; subject_id := old.persona_id;
  else owner_id := new.user_id; subject_id := new.persona_id; end if;
  if tg_table_name = 'record_text_versions' then
    if tg_op = 'INSERT' then invalidate := new.status = 'confirmed';
    elsif tg_op = 'DELETE' then invalidate := old.status = 'confirmed';
    else invalidate := (old.status = 'confirmed' or new.status = 'confirmed')
      and (old.status is distinct from new.status or old.content is distinct from new.content);
    end if;
  elsif tg_table_name = 'records' then
    invalidate := tg_op = 'DELETE' or (tg_op = 'UPDATE' and
      (new.status is distinct from old.status or new.consent_scopes is distinct from old.consent_scopes
        or new.consent_at is distinct from old.consent_at or new.consent_version is distinct from old.consent_version));
  end if;
  if invalidate then
    update public.personas set data_revision = data_revision + 1
      where id = subject_id and user_id = owner_id;
    update public.style_profiles set status = 'stale'
      where persona_id = subject_id and user_id = owner_id and status = 'ready';
  end if;
  if tg_op = 'DELETE' then return old; end if; return new;
end;
$$;
create trigger invalidate_record_text after insert or update or delete on public.record_text_versions
  for each row execute function replica_private.invalidate_personalization();
create trigger invalidate_record after update or delete on public.records
  for each row execute function replica_private.invalidate_personalization();

create function replica_private.guard_text_version() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  perform 1 from public.personas where id = new.persona_id and user_id = new.user_id
    and status = 'active' for update;
  if not found then raise exception 'Persona unavailable' using errcode = 'P0002'; end if;
  perform 1 from public.records where id = new.record_id and user_id = new.user_id
    and persona_id = new.persona_id and status <> 'deleting' for update;
  if not found then raise exception 'Record unavailable' using errcode = 'P0002'; end if;
  if tg_op = 'UPDATE' then
    if (new.id, new.user_id, new.persona_id, new.record_id, new.revision)
      is distinct from (old.id, old.user_id, old.persona_id, old.record_id, old.revision)
    then raise exception 'Text identity is immutable' using errcode = '23514'; end if;
    if old.status <> 'draft' and new.content is distinct from old.content
    then raise exception 'Create a new draft revision to edit confirmed text' using errcode = '23514'; end if;
    if old.status = 'superseded' and new.status <> 'superseded'
    then raise exception 'Superseded text cannot become current again' using errcode = '23514'; end if;
    if old.status = 'confirmed' and new.status not in ('confirmed', 'superseded')
    then raise exception 'Confirmed text cannot become draft' using errcode = '23514'; end if;
  end if;
  if new.status = 'confirmed' then
    perform 1 from public.records where id = new.record_id and user_id = new.user_id
      and status = 'ready' and consent_at is not null and 'personalization' = any(consent_scopes);
    if not found then raise exception 'Record must be ready and consented' using errcode = '23514'; end if;
  end if;
  return new;
end;
$$;
create trigger guard_text_version before insert or update on public.record_text_versions
  for each row execute function replica_private.guard_text_version();

create function replica_private.guard_chunk() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  perform 1 from public.personas where id = new.persona_id and user_id = new.user_id
    and status = 'active' for update;
  if not found then raise exception 'Persona unavailable' using errcode = 'P0002'; end if;
  perform 1 from public.record_text_versions v join public.records r on r.id = v.record_id
    where v.id = new.text_version_id and v.user_id = new.user_id and v.persona_id = new.persona_id
      and v.status = 'confirmed' and r.status = 'ready' and r.consent_at is not null
      and 'personalization' = any(r.consent_scopes);
  if not found then raise exception 'Chunk requires current confirmed text' using errcode = '23514'; end if;
  return new;
end;
$$;
create trigger guard_chunk before insert or update on public.record_chunks
  for each row execute function replica_private.guard_chunk();

-- A deferred check allows profile and source rows to be published in one
-- transaction while preventing a ready profile with missing/stale provenance.
create function replica_private.validate_profile_sources() returns trigger
language plpgsql security invoker set search_path = '' as $$
declare profile_uuid uuid; p public.style_profiles;
begin
  if tg_table_name = 'style_profiles' then profile_uuid := new.id;
  elsif tg_op = 'DELETE' then profile_uuid := old.profile_id;
  else profile_uuid := new.profile_id; end if;
  select * into p from public.style_profiles where id = profile_uuid and status = 'ready';
  if not found then return null; end if;
  perform 1 from public.personas where id = p.persona_id and user_id = p.user_id
    and status = 'active' and data_revision = p.source_revision for update;
  if not found then raise exception 'Profile source revision is stale' using errcode = '23514'; end if;
  if not exists (select 1 from public.profile_sources where profile_id = p.id)
    or exists (select 1 from public.profile_sources s
      join public.record_text_versions v on v.id = s.text_version_id
      join public.records r on r.id = v.record_id
      where s.profile_id = p.id and (v.status <> 'confirmed' or r.status <> 'ready'
        or r.consent_at is null or not ('personalization' = any(r.consent_scopes))))
  then raise exception 'Profile requires current confirmed sources' using errcode = '23514'; end if;
  return null;
end;
$$;
create constraint trigger validate_profile after insert or update on public.style_profiles
  deferrable initially deferred for each row execute function replica_private.validate_profile_sources();
create constraint trigger validate_profile_source after insert or update or delete on public.profile_sources
  deferrable initially deferred for each row execute function replica_private.validate_profile_sources();

create function replica_private.guard_chat_turn() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if (new.id, new.session_id, new.user_id, new.input_mode, new.request_hash)
    is distinct from (old.id, old.session_id, old.user_id, old.input_mode, old.request_hash)
  then raise exception 'Turn identity is immutable' using errcode = '23514'; end if;
  if old.answer_status is not null and new.question is distinct from old.question
  then raise exception 'Question is locked after answer starts' using errcode = '23514'; end if;
  if old.answer_status = 'done' and (new.answer_status is distinct from 'done' or new.answer is distinct from old.answer)
  then raise exception 'Completed answer is immutable' using errcode = '23514'; end if;
  if old.question_status = 'done' and new.question_status <> 'done'
  then raise exception 'Completed question is immutable' using errcode = '23514'; end if;
  if new.llm_attempt is distinct from old.llm_attempt and
    (new.llm_attempt <> old.llm_attempt + 1 or old.answer_status not in ('failed') and old.answer_status is not null)
  then raise exception 'Invalid LLM retry' using errcode = '23514'; end if;
  if new.tts_attempt is distinct from old.tts_attempt and
    (new.tts_attempt <> old.tts_attempt + 1 or old.tts_status not in ('failed') and old.tts_status is not null)
  then raise exception 'Invalid TTS retry' using errcode = '23514'; end if;
  return new;
end;
$$;
create trigger guard_chat_turn before update on public.chat_turns
  for each row execute function replica_private.guard_chat_turn();

create function public.create_chat_turn(p_user_id uuid, p_session_id uuid, p_turn_id uuid,
  p_input_mode text, p_question text default null, p_audio_sha256 text default null)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns; fingerprint text;
begin
  if p_input_mode not in ('text', 'voice') or p_input_mode is null
    or (p_input_mode = 'text' and (p_question is null or length(btrim(p_question)) = 0))
    or (p_input_mode = 'voice' and (p_audio_sha256 is null or p_audio_sha256 !~ '^[0-9a-f]{64}$'))
  then raise exception 'Invalid input or missing audio digest' using errcode = '22023'; end if;
  fingerprint := encode(extensions.digest(p_input_mode || ':' ||
    case when p_input_mode = 'text' then p_question else p_audio_sha256 end, 'sha256'), 'hex');
  perform 1 from public.chat_sessions s join public.personas p on p.id = s.persona_id
    where s.id = p_session_id and s.user_id = p_user_id and p.status = 'active';
  if not found then raise exception 'Session unavailable' using errcode = 'P0002'; end if;
  insert into public.chat_turns (id, session_id, user_id, input_mode, question, question_status, request_hash)
    values (p_turn_id, p_session_id, p_user_id, p_input_mode,
      case when p_input_mode = 'text' then p_question end,
      case when p_input_mode = 'text' then 'done' else 'pending' end, fingerprint)
    on conflict (id) do nothing;
  select * into t from public.chat_turns where id = p_turn_id and user_id = p_user_id;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  if t.session_id <> p_session_id or t.request_hash is distinct from fingerprint
  then raise exception 'Request ID reused with different payload' using errcode = '22023'; end if;
  return t;
end;
$$;

-- Only the caller that successfully claims a stage may invoke STT/LLM/TTS.
-- Return the corresponding *_attempt and pass it back when finishing.
create function public.start_chat_stage(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_model_name text default null, p_with_audio boolean default false)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns;
begin
  select c.* into t from public.chat_turns c join public.chat_sessions s on s.id = c.session_id
    join public.personas p on p.id = s.persona_id
    where c.id = p_turn_id and c.user_id = p_user_id and p.status = 'active' for update of c;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  if p_stage = 'stt' then
    if t.input_mode <> 'voice' or t.answer_status is not null or not
      (t.question_status = 'failed' or (t.question_status = 'pending' and t.stt_attempt = 0))
    then raise exception 'STT already running or completed' using errcode = '55000'; end if;
    update public.chat_turns set question_status = 'pending', question = null,
      stt_attempt = stt_attempt + 1, stt_started_at = clock_timestamp(), stt_ms = null,
      error_message = null, total_ms = null where id = t.id returning * into t;
  elsif p_stage = 'llm' then
    if t.question_status <> 'done' or (t.answer_status is not null and t.answer_status <> 'failed')
      or p_model_name is null or length(btrim(p_model_name)) = 0
    then raise exception 'LLM stage cannot start' using errcode = '55000'; end if;
    update public.chat_turns set answer_status = 'pending', answer = null,
      llm_attempt = llm_attempt + 1, llm_started_at = clock_timestamp(), model_name = p_model_name,
      llm_ms = null, tts_status = null, tts_ms = null, tts_started_at = null,
      audio_requested = p_with_audio, error_message = null, total_ms = null
      where id = t.id returning * into t;
  elsif p_stage = 'tts' then
    if t.answer_status is distinct from 'done' or (t.tts_status is not null and t.tts_status <> 'failed')
    then raise exception 'TTS stage cannot start' using errcode = '55000'; end if;
    update public.chat_turns set tts_status = 'pending', audio_requested = true,
      tts_attempt = tts_attempt + 1, tts_started_at = clock_timestamp(), tts_ms = null,
      error_message = null, total_ms = null where id = t.id returning * into t;
  else raise exception 'Unknown stage' using errcode = '22023'; end if;
  return t;
end;
$$;

create function public.finish_chat_stage(p_user_id uuid, p_turn_id uuid, p_stage text,
  p_attempt integer, p_ok boolean, p_text text default null, p_ms integer default null,
  p_error_code text default null)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare t public.chat_turns;
begin
  select * into t from public.chat_turns where id = p_turn_id and user_id = p_user_id for update;
  if not found then raise exception 'Turn unavailable' using errcode = 'P0002'; end if;
  if p_attempt is null or p_attempt < 1 or p_ok is null or p_ms < 0
  then raise exception 'Invalid stage result' using errcode = '22023'; end if;
  if p_ok and p_stage in ('stt', 'llm') and (p_text is null or length(btrim(p_text)) = 0)
  then raise exception 'Successful text result must not be blank' using errcode = '22023'; end if;
  if p_stage = 'stt' and t.question_status = 'pending' and t.stt_attempt = p_attempt then
    update public.chat_turns set question_status = case when p_ok then 'done' else 'failed' end,
      question = case when p_ok then p_text end, stt_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'stt_failed'), 256) end
      where id = t.id returning * into t;
  elsif p_stage = 'llm' and t.answer_status = 'pending' and t.llm_attempt = p_attempt then
    update public.chat_turns set answer_status = case when p_ok then 'done' else 'failed' end,
      answer = case when p_ok then p_text end, llm_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'llm_failed'), 256) end
      where id = t.id returning * into t;
  elsif p_stage = 'tts' and t.tts_status = 'pending' and t.tts_attempt = p_attempt then
    update public.chat_turns set tts_status = case when p_ok then 'ready' else 'failed' end,
      tts_ms = p_ms,
      error_message = case when p_ok then null else left(coalesce(p_error_code, 'tts_failed'), 256) end
      where id = t.id returning * into t;
  else raise exception 'Stale result or invalid stage' using errcode = '55000'; end if;
  return t;
end;
$$;

create function public.expire_chat_stages(p_user_id uuid, p_started_before timestamptz)
returns integer language plpgsql security invoker set search_path = '' as $$
declare n integer; total integer := 0;
begin
  if p_started_before is null or p_started_before > clock_timestamp()
  then raise exception 'Invalid timeout cutoff' using errcode = '22023'; end if;
  update public.chat_turns set question_status = 'failed', error_message = 'stt_timeout'
    where user_id = p_user_id and question_status = 'pending' and stt_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set answer_status = 'failed', error_message = 'llm_timeout'
    where user_id = p_user_id and answer_status = 'pending' and llm_started_at < p_started_before;
  get diagnostics n = row_count; total := total + n;
  update public.chat_turns set tts_status = 'failed', error_message = 'tts_timeout'
    where user_id = p_user_id and tts_status = 'pending' and tts_started_at < p_started_before;
  get diagnostics n = row_count; return total + n;
end;
$$;

create function public.confirm_record_text(p_user_id uuid, p_text_version_id uuid)
returns public.record_text_versions language plpgsql security invoker set search_path = '' as $$
declare v public.record_text_versions;
begin
  select * into v from public.record_text_versions where id = p_text_version_id and user_id = p_user_id;
  if not found then raise exception 'Text unavailable' using errcode = 'P0002'; end if;
  perform 1 from public.personas where id = v.persona_id and user_id = p_user_id
    and status = 'active' for update;
  if not found then raise exception 'Persona unavailable' using errcode = 'P0002'; end if;
  perform 1 from public.records where id = v.record_id and user_id = p_user_id
    and status = 'ready' and consent_at is not null and 'personalization' = any(consent_scopes) for update;
  if not found then raise exception 'Record is not ready and consented' using errcode = '55000'; end if;
  select * into v from public.record_text_versions where id = p_text_version_id for update;
  if v.status = 'confirmed' then return v; end if;
  if v.status <> 'draft' then raise exception 'Text is superseded' using errcode = '55000'; end if;
  update public.record_text_versions set status = 'superseded'
    where record_id = v.record_id and user_id = p_user_id and status = 'confirmed';
  update public.record_text_versions set status = 'confirmed', confirmed_at = clock_timestamp()
    where id = v.id returning * into v;
  return v;
end;
$$;

create function public.publish_style_profile(p_user_id uuid, p_persona_id uuid,
  p_source_revision bigint, p_content jsonb, p_model_name text, p_source_ids uuid[])
returns public.style_profiles language plpgsql security invoker set search_path = '' as $$
declare p public.personas; result public.style_profiles; expected integer; matched integer;
begin
  select * into p from public.personas where id = p_persona_id and user_id = p_user_id
    and status = 'active' for update;
  if not found then raise exception 'Persona unavailable' using errcode = 'P0002'; end if;
  if p.data_revision is distinct from p_source_revision
  then raise exception 'Personalization input changed; rebuild profile' using errcode = '55000'; end if;
  select count(distinct x) into expected from unnest(p_source_ids) x;
  if expected = 0 then raise exception 'Sources required' using errcode = '22023'; end if;
  select count(*) into matched from public.record_text_versions v join public.records r on r.id = v.record_id
    where v.id = any(p_source_ids) and v.user_id = p_user_id and v.persona_id = p_persona_id
      and v.status = 'confirmed' and r.status = 'ready';
  if matched <> expected then raise exception 'Source unavailable or unconfirmed' using errcode = '55000'; end if;
  update public.style_profiles set status = 'stale' where persona_id = p_persona_id
    and user_id = p_user_id and status = 'ready';
  insert into public.style_profiles(user_id, persona_id, version, source_revision, content, model_name)
    select p_user_id, p_persona_id, coalesce(max(version), 0) + 1, p_source_revision, p_content, p_model_name
      from public.style_profiles where persona_id = p_persona_id returning * into result;
  insert into public.profile_sources(profile_id, text_version_id, user_id, persona_id)
    select result.id, x, p_user_id, p_persona_id from (select distinct unnest(p_source_ids) as x) s;
  return result;
end;
$$;

create function public.search_record_chunks(p_user_id uuid, p_persona_id uuid,
  p_embedding_model text, p_query_embedding extensions.vector,
  p_limit integer default 10, p_min_similarity double precision default 0.5)
returns table(chunk_id uuid, record_id uuid, text_version_id uuid, content text, similarity double precision)
language sql stable security invoker set search_path = '' as $$
  with candidates as materialized (
    select c.*, v.record_id from public.record_chunks c
    join public.record_text_versions v on v.id = c.text_version_id
    join public.records r on r.id = v.record_id
    join public.personas p on p.id = c.persona_id
    where c.user_id = p_user_id and c.persona_id = p_persona_id
      and c.embedding_model = p_embedding_model
      and c.embedding_dimensions = extensions.vector_dims(p_query_embedding)
      and v.status = 'confirmed' and r.status = 'ready' and p.status = 'active'
      and r.consent_at is not null and 'personalization' = any(r.consent_scopes)
  )
  select c.id, c.record_id, c.text_version_id, c.content,
    1 - (c.embedding operator(extensions.<=>) p_query_embedding)
  from candidates c
  where 1 - (c.embedding operator(extensions.<=>) p_query_embedding) >= p_min_similarity
  order by c.embedding operator(extensions.<=>) p_query_embedding, c.id
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$$;

create function public.request_record_deletion(p_user_id uuid, p_record_id uuid)
returns public.deletion_jobs language plpgsql security invoker set search_path = '' as $$
declare r public.records; job public.deletion_jobs;
begin
  select * into r from public.records where id = p_record_id and user_id = p_user_id;
  if not found then raise exception 'Record unavailable' using errcode = 'P0002'; end if;
  perform 1 from public.personas where id = r.persona_id and user_id = p_user_id for update;
  select * into r from public.records where id = p_record_id and user_id = p_user_id for update;
  select * into job from public.deletion_jobs where user_id = p_user_id
    and target_type = 'record' and target_id = r.id and status <> 'completed';
  if found then return job; end if;
  insert into public.deletion_jobs(user_id, target_type, target_id, manifest)
    values(p_user_id, 'record', r.id, jsonb_build_object('bucket_id', r.bucket_id,
      'object_path', r.object_path, 'persona_id', r.persona_id)) returning * into job;
  update public.records set status = 'deleting' where id = r.id and user_id = p_user_id;
  return job;
end;
$$;

create function replica_private.guard_record_delete() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status <> 'deleting' or not exists (select 1 from public.deletion_jobs j
    where j.user_id = old.user_id and j.target_type = 'record' and j.target_id = old.id
      and j.status in ('pending', 'running', 'failed') and not ('storage' = any(j.remaining_steps)))
  then raise exception 'Delete Storage object via API before deleting record metadata' using errcode = '55000'; end if;
  return old;
end;
$$;
create trigger guard_record_delete before delete on public.records
  for each row execute function replica_private.guard_record_delete();

-- RLS: only owner reads; writes are server-only. Policies check parent lifecycle
-- so deletion immediately excludes content from reads and RAG.
do $$
declare t text;
begin
  foreach t in array array['personas', 'chat_sessions', 'chat_turns', 'records',
    'record_text_versions', 'style_profiles', 'profile_sources', 'record_chunks', 'deletion_jobs'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from public, anon, authenticated, service_role', t);
    execute format('grant select on public.%I to authenticated', t);
    execute format('grant select, insert, update, delete on public.%I to service_role', t);
  end loop;
end;
$$;
grant usage, select on sequence public.chat_turns_turn_no_seq to service_role;
create policy personas_owner_read on public.personas for select to authenticated
  using (user_id = (select auth.uid()) and status = 'active');
create policy chat_sessions_owner_read on public.chat_sessions for select to authenticated
  using (user_id = (select auth.uid()) and exists
    (select 1 from public.personas p where p.id = persona_id and p.user_id = chat_sessions.user_id));
create policy chat_turns_owner_read on public.chat_turns for select to authenticated
  using (user_id = (select auth.uid()) and exists
    (select 1 from public.chat_sessions s where s.id = session_id and s.user_id = chat_turns.user_id));
create policy records_owner_read on public.records for select to authenticated
  using (user_id = (select auth.uid()) and status <> 'deleting' and exists
    (select 1 from public.personas p where p.id = persona_id and p.user_id = records.user_id));
create policy record_text_owner_read on public.record_text_versions for select to authenticated
  using (user_id = (select auth.uid()) and exists
    (select 1 from public.records r where r.id = record_id and r.user_id = record_text_versions.user_id));
create policy style_profiles_owner_read on public.style_profiles for select to authenticated
  using (user_id = (select auth.uid()) and status = 'ready' and exists
    (select 1 from public.personas p where p.id = persona_id and p.user_id = style_profiles.user_id));
create policy profile_sources_owner_read on public.profile_sources for select to authenticated
  using (user_id = (select auth.uid()) and exists
    (select 1 from public.style_profiles p where p.id = profile_id and p.user_id = profile_sources.user_id));
create policy record_chunks_owner_read on public.record_chunks for select to authenticated
  using (user_id = (select auth.uid()) and exists
    (select 1 from public.record_text_versions v join public.records r on r.id = v.record_id
      where v.id = text_version_id and v.user_id = record_chunks.user_id
        and v.status = 'confirmed' and r.status = 'ready'));
create policy deletion_jobs_owner_read on public.deletion_jobs for select to authenticated
  using (user_id = (select auth.uid()));

-- Storage bucket must be provisioned as private via Storage API/dashboard.
-- Do not insert/delete Storage metadata with SQL.
create policy replica_records_upload on storage.objects for insert to authenticated
  with check (bucket_id = 'replica-records' and exists
    (select 1 from public.records r where r.user_id = (select auth.uid())
      and r.bucket_id = objects.bucket_id and r.object_path = objects.name and r.status = 'uploading'));
create policy replica_records_read on storage.objects for select to authenticated
  using (bucket_id = 'replica-records' and exists
    (select 1 from public.records r where r.user_id = (select auth.uid())
      and r.bucket_id = objects.bucket_id and r.object_path = objects.name));

-- PostgreSQL grants EXECUTE to PUBLIC by default. Remove it for our functions.
-- No SECURITY DEFINER functions are used.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as signature from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'replica_private' or (n.nspname = 'public' and p.proname in
      ('create_chat_turn', 'start_chat_stage', 'finish_chat_stage', 'expire_chat_stages',
       'confirm_record_text', 'publish_style_profile', 'search_record_chunks', 'request_record_deletion'))
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f.signature);
    execute format('grant execute on function %s to service_role', f.signature);
  end loop;
end;
$$;
notify pgrst, 'reload schema';
