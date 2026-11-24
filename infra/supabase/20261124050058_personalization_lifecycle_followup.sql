-- Follow-up after personalization_lifecycle_121. Preserve the deployed original patch.
begin;
create index personalization_snapshots_persona_fk_idx on public.personalization_snapshots(persona_id,user_id);
create index profile_analysis_nodes_attempt_fk_idx on public.profile_analysis_nodes(operation_id,attempt,user_id,persona_id);
create index record_index_chunks_build_fk_idx on public.record_index_chunks(build_id,user_id,persona_id);

create or replace function public.get_record_reflection(p_user_id uuid, p_record_id uuid)
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
  if found and (a.status in ('failed','unknown') or (a.status='running' and a.deadline<=clock_timestamp())) and exists(select from public.personalization_snapshots s
    where s.id=a.snapshot_id and s.source_stamp=p.source_stamp) then return 'failed'; end if;
  select * into b from public.record_index_builds where persona_id=p.id order by created_at desc,id desc limit 1;
  if found and b.status='failed' and exists(select from public.personalization_snapshots s
    where s.id=b.snapshot_id and s.source_stamp=p.source_stamp) then return 'failed'; end if;
  return 'pending';
end;
$$;

notify pgrst,'reload schema';
commit;
