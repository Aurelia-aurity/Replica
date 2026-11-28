-- #121 DB lifecycle. Requires record_metadata.sql and the existing Replica schema.
begin;
grant usage on schema extensions to service_role;

alter table public.personas add column source_stamp uuid not null default gen_random_uuid();
alter table public.records add column source_format text check (source_format in ('plain', 'kakao', 'audio'));
alter table public.record_text_versions
  add column source_format text check (source_format in ('plain', 'kakao', 'audio')),
  add column source_units jsonb check (jsonb_typeof(source_units) = 'array'),
  add column speaker_set text[],
  add column target_speaker text,
  add column provider_speaker text;
alter table public.style_profiles add column source_snapshot_id uuid;
-- Keep the guard marker even if cleanup cascades away a context binding.
alter table public.chat_turns add column persona_bound_attempt integer check (persona_bound_attempt > 0);

create function replica_private.refresh_source_stamp() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if (new.data_revision, new.status) is distinct from (old.data_revision, old.status) then
    new.source_stamp := gen_random_uuid();
  end if;
  return new;
end;
$$;
create trigger refresh_source_stamp before update on public.personas
  for each row execute function replica_private.refresh_source_stamp();

create function replica_private.guard_source_metadata() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if tg_table_name = 'records' then
    if old.source_format is not null and new.source_format is distinct from old.source_format then
      raise exception 'Original source format is immutable' using errcode = '23514';
    end if;
  elsif old.status <> 'draft' and
    (new.source_format, new.source_units, new.speaker_set, new.target_speaker, new.provider_speaker)
      is distinct from
    (old.source_format, old.source_units, old.speaker_set, old.target_speaker, old.provider_speaker)
  then raise exception 'Create a new text revision to change source metadata' using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger guard_source_metadata before update on public.records
  for each row execute function replica_private.guard_source_metadata();
create trigger guard_source_metadata before update on public.record_text_versions
  for each row execute function replica_private.guard_source_metadata();

create table public.personalization_snapshots (
  id uuid primary key,
  user_id uuid not null,
  persona_id uuid not null,
  source_stamp uuid not null,
  source_revision bigint not null,
  masking_policy_version text not null check (length(btrim(masking_policy_version)) > 0),
  manifest jsonb not null check (jsonb_typeof(manifest) = 'array' and jsonb_array_length(manifest) > 0),
  created_at timestamptz not null default clock_timestamp(),
  unique (id, user_id, persona_id),
  foreign key (persona_id, user_id) references public.personas(id, user_id) on delete cascade
);
create index personalization_snapshots_owner_idx on public.personalization_snapshots(user_id, persona_id);
alter table public.style_profiles add constraint style_profile_snapshot_owner_fk
  foreign key (source_snapshot_id, user_id, persona_id)
  references public.personalization_snapshots(id, user_id, persona_id) on delete cascade;
create index style_profiles_snapshot_owner_idx on public.style_profiles(source_snapshot_id, user_id, persona_id);

create function replica_private.immutable_snapshot() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  raise exception 'Snapshot is immutable' using errcode = '23514';
end;
$$;
create trigger immutable_snapshot before update on public.personalization_snapshots
  for each row execute function replica_private.immutable_snapshot();

create table public.profile_analysis_attempts (
  operation_id uuid not null,
  attempt integer not null check (attempt > 0),
  user_id uuid not null,
  persona_id uuid not null,
  snapshot_id uuid not null,
  runtime jsonb not null check (jsonb_typeof(runtime) = 'object'),
  deadline timestamptz not null,
  status text not null default 'running' check (status in ('running','failed','unknown','completed')),
  published_profile_id uuid, -- Durable commit receipt even if the profile is later removed.
  published_root_node_id uuid,
  error_code text,
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  primary key (operation_id, attempt),
  unique (operation_id),
  unique (persona_id, attempt),
  unique (operation_id, attempt, user_id, persona_id),
  foreign key (snapshot_id, user_id, persona_id)
    references public.personalization_snapshots(id, user_id, persona_id) on delete cascade
);
create index profile_analysis_owner_idx on public.profile_analysis_attempts(user_id, persona_id, attempt desc);
create index profile_analysis_snapshot_idx on public.profile_analysis_attempts(snapshot_id, user_id, persona_id);
create index profile_analysis_receipt_idx on public.profile_analysis_attempts(published_profile_id);

create table public.profile_analysis_nodes (
  operation_id uuid not null,
  attempt integer not null,
  node_id uuid not null,
  request_id uuid not null,
  user_id uuid not null,
  persona_id uuid not null,
  kind text not null check (kind in ('analyze','merge')),
  coverage jsonb not null check (jsonb_typeof(coverage) = 'array'),
  children uuid[] not null default '{}',
  status text not null default 'pending' check (status in ('pending','done','failed','unknown')),
  content jsonb,
  model_name text,
  error_code text,
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  primary key (operation_id, attempt, node_id),
  unique (request_id),
  foreign key (operation_id, attempt, user_id, persona_id)
    references public.profile_analysis_attempts(operation_id, attempt, user_id, persona_id) on delete cascade
);
create index profile_analysis_nodes_owner_idx on public.profile_analysis_nodes(user_id, persona_id);

create table public.record_index_builds (
  id uuid primary key,
  user_id uuid not null,
  persona_id uuid not null,
  snapshot_id uuid not null,
  embedding_model text not null check (length(btrim(embedding_model)) > 0),
  embedding_revision text not null check (length(btrim(embedding_revision)) > 0),
  dimensions integer not null check (dimensions > 0),
  status text not null default 'building' check (status in ('building','ready','retired','failed')),
  created_at timestamptz not null default clock_timestamp(),
  unique (id, user_id, persona_id),
  foreign key (snapshot_id, user_id, persona_id)
    references public.personalization_snapshots(id, user_id, persona_id) on delete cascade
);
create unique index record_index_one_ready_idx on public.record_index_builds(persona_id) where status='ready';
create index record_index_builds_owner_idx on public.record_index_builds(user_id, persona_id);
create index record_index_builds_snapshot_idx on public.record_index_builds(snapshot_id, user_id, persona_id);

create table public.record_index_chunks (
  id uuid primary key,
  build_id uuid not null,
  user_id uuid not null,
  persona_id uuid not null,
  chunk_no integer not null check (chunk_no >= 0),
  content text not null check (length(content) > 0),
  coverage jsonb not null check (jsonb_typeof(coverage) = 'array' and jsonb_array_length(coverage) > 0),
  speaker text,
  speaker_role text not null check (speaker_role in ('persona','provider','other')),
  embedding extensions.vector not null,
  unique (build_id, chunk_no),
  foreign key (build_id, user_id, persona_id)
    references public.record_index_builds(id, user_id, persona_id) on delete cascade,
  check ((embedding operator(extensions.<#>) embedding) < 0)
);
create index record_index_chunks_owner_idx on public.record_index_chunks(user_id, persona_id);

create table public.persona_turn_bindings (
  turn_id uuid not null references public.chat_turns(id) on delete cascade,
  attempt integer not null,
  request_id uuid not null,
  user_id uuid not null,
  persona_id uuid not null,
  snapshot_id uuid not null,
  build_id uuid not null,
  profile_id uuid not null references public.style_profiles(id) on delete cascade,
  primary key (turn_id, attempt),
  foreign key (snapshot_id, user_id, persona_id)
    references public.personalization_snapshots(id, user_id, persona_id) on delete cascade,
  foreign key (build_id, user_id, persona_id)
    references public.record_index_builds(id, user_id, persona_id) on delete cascade
);
create index persona_turn_bindings_owner_idx on public.persona_turn_bindings(user_id, persona_id);
create index persona_turn_bindings_snapshot_idx on public.persona_turn_bindings(snapshot_id, user_id, persona_id);
create index persona_turn_bindings_build_idx on public.persona_turn_bindings(build_id, user_id, persona_id);
create index persona_turn_bindings_profile_idx on public.persona_turn_bindings(profile_id);

-- All lifecycle writers lock the persona before a source, attempt, build or turn row.
create function replica_private.current_snapshot(p_user_id uuid, p_snapshot_id uuid)
returns public.personalization_snapshots
language plpgsql security invoker set search_path = '' as $$
declare s public.personalization_snapshots; p public.personas;
begin
  select * into s from public.personalization_snapshots where id=p_snapshot_id and user_id=p_user_id;
  if not found then raise exception 'Snapshot unavailable' using errcode='P0002'; end if;
  select * into p from public.personas where id=s.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  if p.source_stamp <> s.source_stamp or p.data_revision <> s.source_revision then
    raise exception 'Source changed' using errcode='55000';
  end if;
  return s;
end;
$$;

create function replica_private.current_analysis(p_user_id uuid, p_operation_id uuid, p_attempt integer)
returns public.profile_analysis_attempts
language plpgsql security invoker set search_path = '' as $$
declare a public.profile_analysis_attempts;
begin
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id;
  if not found then raise exception 'Analysis unavailable' using errcode='P0002'; end if;
  perform replica_private.current_snapshot(p_user_id,a.snapshot_id);
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id for update;
  if a.status <> 'running' or a.deadline <= clock_timestamp() or exists (
    select from public.profile_analysis_attempts newer where newer.persona_id=a.persona_id and newer.attempt>a.attempt
  ) then raise exception 'Analysis is no longer current' using errcode='55000'; end if;
  return a;
end;
$$;

-- Coverage is expressed in Unicode code points within a stable source unit.
create function replica_private.coverage_complete(p_manifest jsonb, p_ranges jsonb, p_role text)
returns boolean language sql immutable set search_path = '' as $$
  with wanted as (
    select u->>'id' as id, char_length(u->>'text') as size
      from jsonb_array_elements(p_manifest) u where p_role is null or u->>'role'=p_role
  ), spans as (
    select x->>'unit_id' as id, (x->>'start')::integer as lo, (x->>'end')::integer as hi
      from jsonb_array_elements(p_ranges) x
  ), ordered as (
    select *, row_number() over(partition by id order by lo,hi) as pos,
      lag(hi) over(partition by id order by lo,hi) as previous
      from spans
  )
  select exists(select from wanted)
    and not exists(select from spans s left join wanted w using(id)
      where w.id is null or s.lo is null or s.hi is null or s.lo<0 or s.hi<=s.lo or s.hi>w.size)
    and not exists(select from ordered where (pos=1 and lo<>0) or (pos>1 and lo<>previous))
    and not exists(select from wanted w where
      (select max(s.hi) from spans s where s.id=w.id) is distinct from w.size);
$$;

create function public.confirm_record_text_v2(p_user_id uuid, p_text_version_id uuid,
  p_source_format text, p_units jsonb, p_speakers jsonb)
returns public.record_text_versions
language plpgsql security invoker set search_path = '' as $$
declare
  v public.record_text_versions; r public.records; prior public.record_text_versions;
  item jsonb; units jsonb := '[]'; labels text[]; target text; provider text;
  first_pos integer; last_pos integer := 0; position integer := 0; label text; role text; key text;
begin
  select * into v from public.record_text_versions where id=p_text_version_id and user_id=p_user_id;
  if not found then raise exception 'Text unavailable' using errcode='P0002'; end if;
  perform 1 from public.personas where id=v.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  select * into r from public.records where id=v.record_id and user_id=p_user_id for update;
  select * into v from public.record_text_versions where id=p_text_version_id and user_id=p_user_id for update;
  if r.status<>'ready' or v.status<>'draft' then raise exception 'Text cannot be confirmed' using errcode='55000'; end if;
  if r.source_format='kakao' and p_source_format is distinct from 'kakao' then
    raise exception 'speaker_not_found' using errcode='P1202';
  end if;
  if p_source_format is null or p_source_format not in ('plain','kakao','audio')
    or (r.kind='audio') <> (p_source_format='audio')
    or (r.source_format is not null and r.source_format<>p_source_format)
    or jsonb_typeof(p_units) is distinct from 'array' or jsonb_array_length(p_units)=0
    or jsonb_typeof(p_speakers) is distinct from 'object'
  then raise exception 'Invalid source metadata' using errcode='22023'; end if;

  select * into prior from public.record_text_versions
    where record_id=r.id and status='confirmed' order by revision desc limit 1;
  target := coalesce(prior.target_speaker,r.target_speaker);
  provider := coalesce(prior.provider_speaker,r.provider_speaker);
  for key in select jsonb_object_keys(p_speakers) loop
    if key not in ('target_speaker','provider_speaker') or jsonb_typeof(p_speakers->key) not in ('string','null')
    then raise exception 'Invalid speaker patch' using errcode='22023'; end if;
    if key='target_speaker' then target:=replica_private.trim_input(p_speakers->>key);
    else provider:=replica_private.trim_input(p_speakers->>key); end if;
  end loop;

  if (target is not null and char_length(target) not between 1 and 100)
    or (provider is not null and char_length(provider) not between 1 and 100)
  then raise exception 'Invalid speaker length' using errcode='22023'; end if;

  for item in select value from jsonb_array_elements(p_units) loop
    first_pos := (item->>'start')::integer;
    if jsonb_typeof(item) is distinct from 'object' or first_pos is null or first_pos<last_pos
      or (item->>'end')::integer is null or (item->>'end')::integer<=first_pos
      or (item->>'end')::integer>char_length(v.content)
    then raise exception 'Invalid utterance span' using errcode='22023'; end if;
    if replica_private.trim_input(substring(v.content from first_pos+1
      for (item->>'end')::integer-first_pos))='' then
      raise exception 'Blank source utterance' using errcode='22023';
    end if;
    if item->>'date' is not null then
      if item->>'date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then
        raise exception 'Invalid utterance date' using errcode='22023';
      end if;
      begin
        perform (item->>'date')::date;
      exception when invalid_datetime_format or datetime_field_overflow then
        raise exception 'Invalid utterance date' using errcode='22023';
      end;
    end if;
    label := replica_private.trim_input(item->>'speaker');
    if (p_source_format='kakao' and (label is null or char_length(label) not between 1 and 100))
      or (p_source_format<>'kakao' and label is not null)
    then raise exception 'Invalid utterance speaker' using errcode='22023'; end if;
    if label=target then role:='persona'; elsif label=provider then role:='provider'; else role:='other'; end if;
    if p_source_format<>'kakao' then role:='persona'; end if;
    units := units || jsonb_build_array(jsonb_build_object(
      'id',gen_random_uuid(),'ordinal',position,'start',first_pos,'end',(item->>'end')::integer,
      'speaker',label,'role',role,'date',item->>'date'));
    last_pos := (item->>'end')::integer; position:=position+1;
  end loop;
  select array_agg(distinct u->>'speaker' order by u->>'speaker') filter(where u->>'speaker' is not null)
    into labels from jsonb_array_elements(units) u;
  if p_source_format='kakao' then
    if target is null or (cardinality(labels)>1 and provider is null) then
      raise exception 'speaker_required' using errcode='P1201';
    end if;
    if not (target=any(labels)) or (provider is not null and not(provider=any(labels))) then
      raise exception 'speaker_not_found' using errcode='P1202';
    end if;
    if cardinality(labels)=1 and provider is not null then
      raise exception 'speaker_not_allowed' using errcode='P1203';
    end if;
    if prior.id is not null and prior.speaker_set is distinct from labels and p_speakers='{}'::jsonb then
      raise exception 'speaker_required' using errcode='P1201';
    end if;
    if target=provider then raise exception 'Duplicate speaker' using errcode='22023'; end if;
  elsif target is not null or provider is not null then
    raise exception 'speaker_not_allowed' using errcode='P1203';
  end if;

  update public.records set source_format=p_source_format,target_speaker=target,provider_speaker=provider where id=r.id;
  update public.record_text_versions set source_format=p_source_format,source_units=units,
    speaker_set=labels,target_speaker=target,provider_speaker=provider where id=v.id;
  return public.confirm_record_text(p_user_id,p_text_version_id);
end;
$$;

create function public.capture_personalization_snapshot(p_user_id uuid, p_persona_id uuid,
  p_snapshot_id uuid, p_masking_policy_version text)
returns public.personalization_snapshots
language plpgsql security invoker set search_path = '' as $$
declare p public.personas; s public.personalization_snapshots; manifest jsonb;
begin
  select * into p from public.personas where id=p_persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  if p_snapshot_id is null or p_masking_policy_version is null or length(btrim(p_masking_policy_version))=0
  then raise exception 'Snapshot identity and policy version required' using errcode='22023'; end if;
  select * into s from public.personalization_snapshots where id=p_snapshot_id;
  if found then
    if s.user_id<>p_user_id or s.persona_id<>p_persona_id or s.source_stamp<>p.source_stamp
      or s.masking_policy_version<>p_masking_policy_version
    then raise exception 'Snapshot ID conflict' using errcode='55000'; end if;
    return s;
  end if;
  if exists(select from public.record_text_versions v join public.records r on r.id=v.record_id
    where v.user_id=p_user_id and v.persona_id=p_persona_id and v.status='confirmed' and r.status='ready'
      and r.consent_at is not null and 'personalization'=any(r.consent_scopes)
      and (v.source_units is null or v.source_format is null))
  then raise exception 'Confirmed sources require explicit utterance metadata' using errcode='55000'; end if;
  select jsonb_agg(u || jsonb_build_object('record_id',v.record_id,'text_version_id',v.id,
      'text',substring(v.content from (u->>'start')::integer+1 for (u->>'end')::integer-(u->>'start')::integer))
      order by r.id,v.id,(u->>'ordinal')::integer)
    into manifest
    from public.records r join public.record_text_versions v on v.record_id=r.id
      cross join lateral jsonb_array_elements(v.source_units) u
    where r.user_id=p_user_id and r.persona_id=p_persona_id and r.status='ready'
      and r.consent_at is not null and 'personalization'=any(r.consent_scopes) and v.status='confirmed';
  if manifest is null or not exists(select from jsonb_array_elements(manifest) u where u->>'role'='persona')
  then raise exception 'No confirmed persona utterances' using errcode='22023'; end if;
  insert into public.personalization_snapshots(id,user_id,persona_id,source_stamp,source_revision,masking_policy_version,manifest)
    values(p_snapshot_id,p_user_id,p_persona_id,p.source_stamp,p.data_revision,p_masking_policy_version,manifest)
    returning * into s;
  return s;
end;
$$;

-- Call before adopting a changed role/masking policy outside text confirmation.
create function public.advance_personalization_source(p_user_id uuid, p_persona_id uuid, p_expected_stamp uuid)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare p public.personas; stamp uuid;
begin
  select * into p from public.personas where id=p_persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  if p.source_stamp is distinct from p_expected_stamp then raise exception 'Source changed' using errcode='55000'; end if;
  update public.personas set data_revision=data_revision+1 where id=p.id returning source_stamp into stamp;
  update public.style_profiles set status='stale' where persona_id=p.id and user_id=p_user_id and status='ready';
  return stamp;
end;
$$;

create function public.start_profile_analysis(p_user_id uuid, p_snapshot_id uuid,
  p_operation_id uuid, p_deadline timestamptz, p_runtime jsonb)
returns public.profile_analysis_attempts
language plpgsql security invoker set search_path = '' as $$
declare s public.personalization_snapshots; a public.profile_analysis_attempts; key text; next_attempt integer;
begin
  s:=replica_private.current_snapshot(p_user_id,p_snapshot_id);
  if p_operation_id is null or p_deadline is null or p_deadline<=clock_timestamp()
    or jsonb_typeof(p_runtime) is distinct from 'object'
    or not (p_runtime ?& array['model','tokenizer','template'])
  then raise exception 'Invalid analysis binding' using errcode='22023'; end if;
  foreach key in array array['model','tokenizer','template'] loop
    if jsonb_typeof(p_runtime->key) is distinct from 'string' or length(btrim(p_runtime->>key))=0
    then raise exception 'Runtime revisions required' using errcode='22023'; end if;
  end loop;
  select * into a from public.profile_analysis_attempts where operation_id=p_operation_id;
  if found then
    if a.user_id<>p_user_id or a.snapshot_id<>p_snapshot_id or a.runtime<>p_runtime or a.deadline<>p_deadline
    then raise exception 'Operation ID conflict' using errcode='55000'; end if;
    return a;
  end if;
  select coalesce(max(attempt),0)+1 into next_attempt from public.profile_analysis_attempts where persona_id=s.persona_id;
  insert into public.profile_analysis_attempts(operation_id,attempt,user_id,persona_id,snapshot_id,runtime,deadline)
    values(p_operation_id,next_attempt,p_user_id,s.persona_id,s.id,p_runtime,p_deadline) returning * into a;
  return a;
end;
$$;

create function public.start_profile_analysis_node(p_user_id uuid, p_operation_id uuid,
  p_attempt integer, p_node_id uuid, p_request_id uuid, p_kind text, p_ranges jsonb, p_children uuid[])
returns public.profile_analysis_nodes
language plpgsql security invoker set search_path = '' as $$
declare a public.profile_analysis_attempts; s public.personalization_snapshots;
  n public.profile_analysis_nodes; coverage jsonb; children uuid[]; child uuid;
begin
  a:=replica_private.current_analysis(p_user_id,p_operation_id,p_attempt);
  s:=replica_private.current_snapshot(p_user_id,a.snapshot_id);
  if p_node_id is null or p_request_id is null or p_kind is null or p_kind not in ('analyze','merge')
    or jsonb_typeof(p_ranges) is distinct from 'array' or p_children is null
  then raise exception 'Invalid node' using errcode='22023'; end if;
  children:=p_children;
  coverage:=p_ranges;
  if p_kind='analyze' then
    if cardinality(children)<>0 or jsonb_array_length(coverage)=0 or exists (
      select from jsonb_array_elements(coverage) r
      left join lateral (select u from jsonb_array_elements(s.manifest) u
        where u->>'id'=r->>'unit_id' and u->>'role'='persona') source on true
      where source.u is null or (r->>'start')::integer is null or (r->>'end')::integer is null
        or (r->>'start')::integer<0 or (r->>'end')::integer<=(r->>'start')::integer
        or (r->>'end')::integer>char_length(source.u->>'text')
    ) then raise exception 'Invalid node input ranges' using errcode='22023'; end if;
  else
    if cardinality(children) not between 2 and 8 or jsonb_array_length(p_ranges)<>0
      or p_node_id=any(children) or cardinality(children)<>(select count(distinct c) from unnest(children) c)
    then raise exception 'Invalid merge children' using errcode='22023'; end if;
    coverage:='[]'::jsonb;
    foreach child in array children loop
      select * into n from public.profile_analysis_nodes
        where operation_id=p_operation_id and attempt=p_attempt and node_id=child and status='done';
      if not found then raise exception 'Merge child is not complete' using errcode='55000'; end if;
      coverage:=coverage||n.coverage;
    end loop;
  end if;
  select * into n from public.profile_analysis_nodes
    where operation_id=p_operation_id and attempt=p_attempt and node_id=p_node_id;
  if found then
    if n.request_id<>p_request_id or n.kind<>p_kind or n.coverage<>coverage or n.children<>children
    then raise exception 'Node ID conflict' using errcode='55000'; end if;
    return n;
  end if;
  insert into public.profile_analysis_nodes(operation_id,attempt,node_id,request_id,user_id,persona_id,kind,coverage,children)
    values(p_operation_id,p_attempt,p_node_id,p_request_id,p_user_id,a.persona_id,p_kind,coverage,children)
    returning * into n;
  return n;
end;
$$;

create function public.finish_profile_analysis_node(p_user_id uuid, p_operation_id uuid, p_attempt integer,
  p_node_id uuid, p_request_id uuid, p_runtime jsonb, p_status text, p_content jsonb,
  p_model_name text, p_error_code text)
returns public.profile_analysis_nodes
language plpgsql security invoker set search_path = '' as $$
declare a public.profile_analysis_attempts; n public.profile_analysis_nodes;
begin
  a:=replica_private.current_analysis(p_user_id,p_operation_id,p_attempt);
  if p_runtime is distinct from a.runtime or p_status is null or p_status not in ('done','failed','unknown')
    or (p_status='done' and (jsonb_typeof(p_content) is distinct from 'object' or p_content='{}'::jsonb
      or p_model_name is null or length(btrim(p_model_name))=0 or p_error_code is not null))
    or (p_status<>'done' and p_content is not null)
    or (p_error_code is not null and p_error_code !~ '^[a-zA-Z0-9_.-]{1,64}$')
  then raise exception 'Invalid node result binding' using errcode='22023'; end if;
  select * into n from public.profile_analysis_nodes
    where operation_id=p_operation_id and attempt=p_attempt and node_id=p_node_id and user_id=p_user_id for update;
  if not found then raise exception 'Node unavailable' using errcode='P0002'; end if;
  if n.request_id is distinct from p_request_id then raise exception 'Request ID changed' using errcode='55000'; end if;
  if n.status<>'pending' then
    if (n.status,n.content,n.model_name,n.error_code) is distinct from (p_status,p_content,p_model_name,p_error_code)
    then raise exception 'Node result is immutable' using errcode='55000'; end if;
    return n;
  end if;
  update public.profile_analysis_nodes set status=p_status,content=p_content,model_name=p_model_name,
    error_code=p_error_code,finished_at=clock_timestamp()
    where operation_id=p_operation_id and attempt=p_attempt and node_id=p_node_id returning * into n;
  if p_status<>'done' then
    update public.profile_analysis_attempts set status=p_status,error_code=p_error_code,finished_at=clock_timestamp()
      where operation_id=p_operation_id and attempt=p_attempt;
  end if;
  return n;
end;
$$;

create function replica_private.guard_profile_binding() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.status='ready' and new.source_snapshot_id is null and exists(
    select from public.profile_analysis_attempts a where a.persona_id=new.persona_id
  ) then raise exception 'Use the current analysis publication function' using errcode='55000'; end if;
  return new;
end;
$$;
create trigger guard_profile_binding before insert or update on public.style_profiles
  for each row execute function replica_private.guard_profile_binding();

create function public.publish_analysis_profile(p_user_id uuid, p_operation_id uuid, p_attempt integer, p_root_node_id uuid)
returns public.style_profiles
language plpgsql security invoker set search_path = '' as $$
declare a public.profile_analysis_attempts; s public.personalization_snapshots; n public.profile_analysis_nodes;
  result public.style_profiles; source_ids uuid[];
begin
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id;
  if not found then raise exception 'Analysis unavailable' using errcode='P0002'; end if;
  perform 1 from public.personas where id=a.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id;
  if a.status='completed' then
    if a.published_root_node_id is distinct from p_root_node_id then
      raise exception 'Publication root changed' using errcode='55000';
    end if;
    select * into result from public.style_profiles where id=a.published_profile_id and user_id=p_user_id;
    if not found then raise exception 'Published profile was removed; inspect receipt' using errcode='55000'; end if;
    return result;
  end if;
  a:=replica_private.current_analysis(p_user_id,p_operation_id,p_attempt);
  s:=replica_private.current_snapshot(p_user_id,a.snapshot_id);
  select * into n from public.profile_analysis_nodes
    where operation_id=p_operation_id and attempt=p_attempt and node_id=p_root_node_id and status='done';
  if not found or not replica_private.coverage_complete(s.manifest,n.coverage,'persona')
  then raise exception 'Final result does not cover the snapshot exactly once' using errcode='55000'; end if;
  select array_agg(distinct (u->>'text_version_id')::uuid) into source_ids from jsonb_array_elements(s.manifest) u;
  update public.style_profiles set status='stale' where persona_id=a.persona_id and user_id=p_user_id and status='ready';
  insert into public.style_profiles(user_id,persona_id,version,source_revision,content,model_name,source_snapshot_id)
    select p_user_id,a.persona_id,coalesce(max(version),0)+1,s.source_revision,n.content,n.model_name,s.id
      from public.style_profiles where persona_id=a.persona_id returning * into result;
  insert into public.profile_sources(profile_id,text_version_id,user_id,persona_id)
    select result.id,id,p_user_id,a.persona_id from unnest(source_ids) id;
  update public.profile_analysis_attempts set status='completed',published_profile_id=result.id,
    published_root_node_id=p_root_node_id,finished_at=clock_timestamp()
    where operation_id=p_operation_id and attempt=p_attempt;
  return result;
end;
$$;

create function public.get_profile_analysis_result(p_user_id uuid, p_operation_id uuid, p_attempt integer)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare a public.profile_analysis_attempts; outcome text;
begin
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id;
  if not found then raise exception 'Analysis unavailable; absence is not proof of rollback' using errcode='P0002'; end if;
  perform 1 from public.personas where id=a.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  select * into a from public.profile_analysis_attempts
    where operation_id=p_operation_id and attempt=p_attempt and user_id=p_user_id;
  outcome:=case when a.status='completed' then 'committed'
    when a.status in ('failed','unknown') or a.deadline<=clock_timestamp() or exists(
      select from public.profile_analysis_attempts newer where newer.persona_id=a.persona_id and newer.attempt>a.attempt
    ) then 'not_committed' else 'unresolved' end;
  return jsonb_build_object('operation_id',a.operation_id,'attempt',a.attempt,'status',a.status,
    'publication_state',outcome,'profile_id',a.published_profile_id,'snapshot_id',a.snapshot_id);
end;
$$;

create function public.start_record_index_build(p_user_id uuid, p_snapshot_id uuid, p_build_id uuid,
  p_embedding_model text, p_embedding_revision text, p_dimensions integer)
returns public.record_index_builds
language plpgsql security invoker set search_path = '' as $$
declare s public.personalization_snapshots; b public.record_index_builds;
begin
  s:=replica_private.current_snapshot(p_user_id,p_snapshot_id);
  if p_build_id is null or p_embedding_model is null or length(btrim(p_embedding_model))=0
    or p_embedding_revision is null or length(btrim(p_embedding_revision))=0 or p_dimensions is null or p_dimensions<1
  then raise exception 'Invalid encoder binding' using errcode='22023'; end if;
  select * into b from public.record_index_builds where id=p_build_id;
  if found then
    if (b.user_id,b.snapshot_id,b.embedding_model,b.embedding_revision,b.dimensions)
      is distinct from (p_user_id,p_snapshot_id,p_embedding_model,p_embedding_revision,p_dimensions)
    then raise exception 'Build ID conflict' using errcode='55000'; end if;
    return b;
  end if;
  insert into public.record_index_builds(id,user_id,persona_id,snapshot_id,embedding_model,embedding_revision,dimensions)
    values(p_build_id,p_user_id,s.persona_id,s.id,p_embedding_model,p_embedding_revision,p_dimensions) returning * into b;
  return b;
end;
$$;

create function public.append_record_index_chunks(p_user_id uuid, p_build_id uuid, p_chunks jsonb)
returns integer language plpgsql security invoker set search_path = '' as $$
declare b public.record_index_builds; s public.personalization_snapshots; chunk jsonb; span jsonb; unit jsonb;
  first_unit jsonb; text_body text; vec extensions.vector; count_rows integer:=0;
  existing public.record_index_chunks; previous_unit jsonb; previous_end integer;
begin
  select * into b from public.record_index_builds where id=p_build_id and user_id=p_user_id;
  if not found then raise exception 'Build unavailable' using errcode='P0002'; end if;
  s:=replica_private.current_snapshot(p_user_id,b.snapshot_id);
  select * into b from public.record_index_builds where id=p_build_id and user_id=p_user_id for update;
  if b.status<>'building' then raise exception 'Build is immutable' using errcode='55000'; end if;
  if jsonb_typeof(p_chunks) is distinct from 'array' or jsonb_array_length(p_chunks)=0
  then raise exception 'Chunk batch required' using errcode='22023'; end if;
  for chunk in select value from jsonb_array_elements(p_chunks) loop
    if chunk->>'id' is null or (chunk->>'chunk_no')::integer is null or (chunk->>'chunk_no')::integer<0
      or jsonb_typeof(chunk->'coverage') is distinct from 'array' or jsonb_array_length(chunk->'coverage')=0
      or jsonb_typeof(chunk->'embedding') is distinct from 'array'
    then raise exception 'Invalid chunk' using errcode='22023'; end if;
    first_unit:=null; previous_unit:=null; previous_end:=null; text_body:='';
    for span in select value from jsonb_array_elements(chunk->'coverage') loop
      select u into unit from jsonb_array_elements(s.manifest) u where u->>'id'=span->>'unit_id';
      if not found or (span->>'start')::integer is null or (span->>'end')::integer is null
        or (span->>'start')::integer<0 or (span->>'end')::integer<=(span->>'start')::integer
        or (span->>'end')::integer>char_length(unit->>'text')
      then raise exception 'Invalid chunk source range' using errcode='22023'; end if;
      if first_unit is null then first_unit:=unit;
      elsif (unit->>'text_version_id',unit->>'speaker',unit->>'role',unit->>'date')
        is distinct from (first_unit->>'text_version_id',first_unit->>'speaker',first_unit->>'role',first_unit->>'date')
      then raise exception 'Chunk mixes source roles or versions' using errcode='22023'; end if;
      if previous_unit is not null and (
        (unit->>'id'=previous_unit->>'id' and (span->>'start')::integer<>previous_end) or
        (unit->>'id'<>previous_unit->>'id' and (
          (unit->>'ordinal')::integer<>(previous_unit->>'ordinal')::integer+1
          or previous_end<>char_length(previous_unit->>'text') or (span->>'start')::integer<>0))
      ) then raise exception 'Chunk ranges are not adjacent' using errcode='22023'; end if;
      text_body:=text_body||substring(unit->>'text' from (span->>'start')::integer+1
        for (span->>'end')::integer-(span->>'start')::integer);
      previous_unit:=unit; previous_end:=(span->>'end')::integer;
    end loop;
    vec:=(chunk->'embedding')::text::extensions.vector;
    if text_body is distinct from chunk->>'content' or extensions.vector_dims(vec)<>b.dimensions
      or (vec operator(extensions.<#>) vec)>=0
    then raise exception 'Chunk content or encoder dimensions differ' using errcode='22023'; end if;
    select * into existing from public.record_index_chunks where id=(chunk->>'id')::uuid;
    if found then
      if (existing.user_id,existing.build_id,existing.chunk_no,existing.content,existing.coverage,existing.embedding::text)
        is distinct from (p_user_id,p_build_id,(chunk->>'chunk_no')::integer,text_body,chunk->'coverage',vec::text)
      then raise exception 'Chunk ID conflict' using errcode='55000'; end if;
    else
      insert into public.record_index_chunks(id,build_id,user_id,persona_id,chunk_no,content,coverage,speaker,speaker_role,embedding)
        values((chunk->>'id')::uuid,b.id,p_user_id,b.persona_id,(chunk->>'chunk_no')::integer,
          text_body,chunk->'coverage',first_unit->>'speaker',first_unit->>'role',vec);
    end if;
    count_rows:=count_rows+1;
  end loop;
  return count_rows;
end;
$$;

create function public.activate_record_index_build(p_user_id uuid, p_build_id uuid)
returns public.record_index_builds
language plpgsql security invoker set search_path = '' as $$
declare b public.record_index_builds; s public.personalization_snapshots; ranges jsonb;
begin
  select * into b from public.record_index_builds where id=p_build_id and user_id=p_user_id;
  if not found then raise exception 'Build unavailable' using errcode='P0002'; end if;
  s:=replica_private.current_snapshot(p_user_id,b.snapshot_id);
  select * into b from public.record_index_builds where id=p_build_id and user_id=p_user_id for update;
  if b.status='ready' then return b; end if;
  if b.status<>'building' then raise exception 'Build is not pending' using errcode='55000'; end if;
  select jsonb_agg(r) into ranges from public.record_index_chunks c
    cross join lateral jsonb_array_elements(c.coverage) r where c.build_id=b.id;
  if ranges is null or not replica_private.coverage_complete(s.manifest,ranges,null)
  then raise exception 'Build does not cover the snapshot exactly once' using errcode='55000'; end if;
  update public.record_index_builds set status='retired' where persona_id=b.persona_id and status='ready';
  update public.record_index_builds set status='ready' where id=b.id returning * into b;
  return b;
end;
$$;

create function public.fail_record_index_build(p_user_id uuid, p_build_id uuid)
returns public.record_index_builds
language plpgsql security invoker set search_path = '' as $$
declare b public.record_index_builds;
begin
  select * into b from public.record_index_builds where id=p_build_id and user_id=p_user_id;
  if not found then raise exception 'Build unavailable' using errcode='P0002'; end if;
  perform 1 from public.personas where id=b.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  update public.record_index_builds set status='failed' where id=b.id and status in ('building','failed') returning * into b;
  if not found then raise exception 'An active build cannot be failed' using errcode='55000'; end if;
  return b;
end;
$$;

create function public.read_persona_context(p_user_id uuid, p_persona_id uuid,
  p_embedding_model text, p_embedding_revision text, p_query_embedding extensions.vector,
  p_limit integer, p_min_similarity double precision)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare p public.personas; b public.record_index_builds; s public.personalization_snapshots;
  profile public.style_profiles; evidence jsonb;
begin
  select * into p from public.personas where id=p_persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  if p_query_embedding is null or (p_query_embedding operator(extensions.<#>) p_query_embedding)>=0
    or p_limit is null or p_limit not between 1 and 10 or p_min_similarity is null
    or p_min_similarity not between -1 and 1
  then raise exception 'Invalid query vector or limit' using errcode='22023'; end if;
  select * into b from public.record_index_builds where persona_id=p_persona_id and user_id=p_user_id and status='ready';
  if not found then raise exception 'No active index' using errcode='55000'; end if;
  s:=replica_private.current_snapshot(p_user_id,b.snapshot_id);
  if (b.embedding_model,b.embedding_revision,b.dimensions) is distinct from
    (p_embedding_model,p_embedding_revision,extensions.vector_dims(p_query_embedding))
  then raise exception 'Encoder binding changed' using errcode='22023'; end if;
  select * into profile from public.style_profiles
    where persona_id=p_persona_id and user_id=p_user_id and status='ready' and source_snapshot_id=s.id;
  if not found then raise exception 'Profile and index do not share a snapshot' using errcode='55000'; end if;
  select coalesce(jsonb_agg(to_jsonb(matched) order by matched.similarity desc,matched.chunk_id),'[]') into evidence from (
    select c.id as chunk_id,c.content,c.speaker,c.speaker_role,c.coverage,
      (select u->>'record_id' from jsonb_array_elements(s.manifest) u
        where u->>'id'=c.coverage->0->>'unit_id') as record_id,
      (select u->>'text_version_id' from jsonb_array_elements(s.manifest) u
        where u->>'id'=c.coverage->0->>'unit_id') as text_version_id,
      1-(c.embedding operator(extensions.<=>) p_query_embedding) as similarity
    from public.record_index_chunks c where c.build_id=b.id
      and 1-(c.embedding operator(extensions.<=>) p_query_embedding)>=p_min_similarity
    order by similarity desc,c.id limit p_limit
  ) matched;
  return jsonb_build_object('source_stamp',s.source_stamp,'source_revision',s.source_revision,
    'source_snapshot_id',s.id,'index_build_id',b.id,'profile',to_jsonb(profile),'evidence',evidence);
end;
$$;

create function public.get_record_reflection(p_user_id uuid, p_record_id uuid)
returns text language plpgsql security invoker set search_path = '' as $$
declare r public.records; p public.personas; v public.record_text_versions;
  b public.record_index_builds; a public.profile_analysis_attempts; snapshot uuid;
begin
  select * into r from public.records where id=p_record_id and user_id=p_user_id and status<>'deleting';
  if not found then raise exception 'Record unavailable' using errcode='P0002'; end if;
  select * into p from public.personas where id=r.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  select * into r from public.records where id=p_record_id and user_id=p_user_id and status<>'deleting';
  if not found then raise exception 'Record unavailable' using errcode='P0002'; end if;
  select * into v from public.record_text_versions where record_id=r.id and status='confirmed';
  if not found or v.source_units is null or r.status<>'ready' or r.consent_at is null
    or not('personalization'=any(r.consent_scopes)) then return 'none'; end if;
  select s.id into snapshot from public.personalization_snapshots s
    join public.record_index_builds active on active.snapshot_id=s.id and active.status='ready'
    join public.style_profiles profile on profile.source_snapshot_id=s.id and profile.status='ready'
    join public.profile_sources source on source.profile_id=profile.id and source.text_version_id=v.id
    where s.persona_id=p.id and s.source_stamp=p.source_stamp and s.source_revision=p.data_revision;
  if found then return 'reflected'; end if;
  select * into a from public.profile_analysis_attempts where persona_id=p.id order by attempt desc limit 1;
  if found and a.status in ('failed','unknown') and exists(select from public.personalization_snapshots s
    where s.id=a.snapshot_id and s.source_stamp=p.source_stamp) then return 'failed'; end if;
  select * into b from public.record_index_builds where persona_id=p.id order by created_at desc,id desc limit 1;
  if found and b.status='failed' and exists(select from public.personalization_snapshots s
    where s.id=b.snapshot_id and s.source_stamp=p.source_stamp) then return 'failed'; end if;
  return 'pending';
end;
$$;

create function public.bind_persona_chat_turn(p_user_id uuid, p_turn_id uuid, p_request_id uuid,
  p_snapshot_id uuid, p_build_id uuid, p_profile_id uuid)
returns public.persona_turn_bindings
language plpgsql security invoker set search_path = '' as $$
declare s public.personalization_snapshots; t public.chat_turns; binding public.persona_turn_bindings;
begin
  s:=replica_private.current_snapshot(p_user_id,p_snapshot_id);
  select * into t from public.chat_turns where id=p_turn_id and user_id=p_user_id for update;
  if not found or not exists(select from public.chat_sessions session
    where session.id=t.session_id and session.persona_id=s.persona_id and session.user_id=p_user_id)
  then raise exception 'Turn unavailable' using errcode='P0002'; end if;
  if t.answer_status is distinct from 'pending' or t.llm_request_id is distinct from p_request_id
    or not exists(select from public.record_index_builds
      where id=p_build_id and snapshot_id=s.id and user_id=p_user_id and status='ready')
    or not exists(select from public.style_profiles
      where id=p_profile_id and source_snapshot_id=s.id and user_id=p_user_id and status='ready')
  then raise exception 'Context or LLM attempt changed' using errcode='55000'; end if;
  select * into binding from public.persona_turn_bindings where turn_id=t.id and attempt=t.llm_attempt;
  if found then
    if (binding.request_id,binding.snapshot_id,binding.build_id,binding.profile_id)
      is distinct from (p_request_id,p_snapshot_id,p_build_id,p_profile_id)
    then raise exception 'Turn input binding is immutable' using errcode='55000'; end if;
    return binding;
  end if;
  insert into public.persona_turn_bindings(turn_id,attempt,request_id,user_id,persona_id,snapshot_id,build_id,profile_id)
    values(t.id,t.llm_attempt,p_request_id,p_user_id,s.persona_id,s.id,p_build_id,p_profile_id) returning * into binding;
  update public.chat_turns set persona_bound_attempt=t.llm_attempt where id=t.id and user_id=p_user_id;
  return binding;
end;
$$;

create function public.check_persona_chat_turn(p_user_id uuid, p_turn_id uuid, p_request_id uuid, p_attempt integer)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare binding public.persona_turn_bindings;
begin
  select * into binding from public.persona_turn_bindings
    where turn_id=p_turn_id and attempt=p_attempt and user_id=p_user_id and request_id=p_request_id;
  if not found then raise exception 'Turn binding unavailable' using errcode='P0002'; end if;
  perform replica_private.current_snapshot(p_user_id,binding.snapshot_id);
  if not exists(select from public.chat_turns where id=p_turn_id and user_id=p_user_id
      and llm_attempt=p_attempt and llm_request_id=p_request_id and answer_status='pending')
    or not exists(select from public.record_index_builds where id=binding.build_id and status='ready')
    or not exists(select from public.style_profiles where id=binding.profile_id and status='ready'
      and source_snapshot_id=binding.snapshot_id)
  then raise exception 'Context or LLM attempt changed' using errcode='55000'; end if;
  return true;
end;
$$;

create function replica_private.guard_persona_chat_result() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.answer_status='done' and old.answer_status is distinct from 'done'
    and new.persona_bound_attempt=new.llm_attempt then
    perform public.check_persona_chat_turn(new.user_id,new.id,new.llm_request_id,new.llm_attempt);
  end if;
  return new;
end;
$$;
create trigger guard_persona_chat_result before update on public.chat_turns
  for each row execute function replica_private.guard_persona_chat_result();

create function public.finish_persona_chat_turn(p_user_id uuid, p_turn_id uuid, p_request_id uuid, p_attempt integer,
  p_ok boolean, p_text text, p_ms integer, p_error_code text, p_total_ms integer, p_finish_reason text)
returns public.chat_turns language plpgsql security invoker set search_path = '' as $$
declare binding public.persona_turn_bindings;
begin
  select * into binding from public.persona_turn_bindings
    where turn_id=p_turn_id and attempt=p_attempt and user_id=p_user_id and request_id=p_request_id;
  if not found then raise exception 'Turn binding unavailable' using errcode='P0002'; end if;
  perform 1 from public.personas where id=binding.persona_id and user_id=p_user_id and status='active' for update;
  if not found then raise exception 'Persona unavailable' using errcode='P0002'; end if;
  if p_ok then perform public.check_persona_chat_turn(p_user_id,p_turn_id,p_request_id,p_attempt); end if;
  return public.finish_chat_stage_v2(p_user_id,p_turn_id,'llm',p_attempt,p_ok,p_text,p_ms,p_error_code,
    p_total_ms,p_finish_reason,p_request_id);
end;
$$;

-- Old readers and writers keep their original signatures. These new RPCs are server-only.
do $$
declare table_name text; fn record;
begin
  foreach table_name in array array['personalization_snapshots','profile_analysis_attempts','profile_analysis_nodes',
    'record_index_builds','record_index_chunks','persona_turn_bindings'] loop
    execute format('alter table public.%I enable row level security',table_name);
    execute format('revoke all on public.%I from public,anon,authenticated,service_role',table_name);
    execute format('grant select on public.%I to authenticated',table_name);
    execute format('grant select,insert,update,delete on public.%I to service_role',table_name);
  end loop;
  for fn in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where (n.nspname='public' and p.proname in (
      'confirm_record_text_v2','capture_personalization_snapshot','advance_personalization_source','start_profile_analysis',
      'start_profile_analysis_node','finish_profile_analysis_node','publish_analysis_profile',
      'get_profile_analysis_result','start_record_index_build','append_record_index_chunks',
      'activate_record_index_build','fail_record_index_build','read_persona_context','get_record_reflection',
      'bind_persona_chat_turn','check_persona_chat_turn','finish_persona_chat_turn'))
    or (n.nspname='replica_private' and p.proname in (
      'refresh_source_stamp','guard_source_metadata','immutable_snapshot','current_snapshot',
      'current_analysis','coverage_complete','guard_profile_binding','guard_persona_chat_result'))
  loop
    execute format('revoke all on function %s from public,anon,authenticated',fn.signature);
    execute format('grant execute on function %s to service_role',fn.signature);
  end loop;
end;
$$;

create policy snapshot_owner_current on public.personalization_snapshots for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.personas p
    where p.id=personalization_snapshots.persona_id and p.user_id=personalization_snapshots.user_id and p.status='active'
      and p.source_stamp=personalization_snapshots.source_stamp)
);
create policy analysis_owner_current on public.profile_analysis_attempts for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.personalization_snapshots s where s.id=snapshot_id)
);
create policy analysis_node_owner_current on public.profile_analysis_nodes for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.profile_analysis_attempts a
    where a.operation_id=profile_analysis_nodes.operation_id and a.attempt=profile_analysis_nodes.attempt)
);
create policy index_build_owner_current on public.record_index_builds for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.personalization_snapshots s where s.id=snapshot_id)
);
create policy index_chunk_owner_current on public.record_index_chunks for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.record_index_builds b where b.id=build_id)
);
create policy turn_binding_owner_current on public.persona_turn_bindings for select to authenticated using (
  user_id=(select auth.uid()) and exists(select from public.personalization_snapshots s where s.id=snapshot_id)
);

notify pgrst,'reload schema';
commit;
