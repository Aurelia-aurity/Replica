-- First forward patch for #121. Existing rows keep NULL and existing callers may omit the new columns.
begin;

alter table public.personas
  add column display_name text,
  add column provider_address text,
  add constraint personas_display_name_valid check (
    char_length(display_name) between 1 and 50 and display_name !~ '^[[:space:]]|[[:space:]]$'
  ),
  add constraint personas_provider_address_valid check (
    char_length(provider_address) between 1 and 50 and provider_address !~ '^[[:space:]]|[[:space:]]$'
  );

alter table public.records
  add column provider_speaker text,
  add constraint records_provider_speaker_valid check (
    char_length(provider_speaker) between 1 and 100 and provider_speaker !~ '^[[:space:]]|[[:space:]]$'
  );

-- The backend supplies a verified owner ID. An omitted key preserves its value; JSON null clears it.
-- Only these two columns are updated, so editing an address never advances data_revision.
create function public.update_persona_addresses(p_user_id uuid, p_persona_id uuid, p_fields jsonb)
returns public.personas
language plpgsql security invoker set search_path = ''
as $$
declare
  v_field text;
  v_value text;
  v_display_name text;
  v_provider_address text;
  v_persona public.personas;
begin
  if jsonb_typeof(p_fields) is distinct from 'object' or p_fields = '{}'::jsonb then
    raise exception 'Expected a non-empty address patch' using errcode = '22023';
  end if;

  for v_field in select jsonb_object_keys(p_fields) loop
    if v_field not in ('display_name', 'provider_address') or
      jsonb_typeof(p_fields -> v_field) not in ('string', 'null')
    then
      raise exception 'Invalid address patch field' using errcode = '22023';
    end if;

    v_value := regexp_replace(p_fields ->> v_field, '^[[:space:]]+|[[:space:]]+$', '', 'g');
    if v_value is not null and char_length(v_value) not between 1 and 50 then
      raise exception 'Address must contain 1 to 50 characters' using errcode = '22023';
    end if;

    if v_field = 'display_name' then
      v_display_name := v_value;
    else
      v_provider_address := v_value;
    end if;
  end loop;

  update public.personas
  set display_name = case when p_fields ? 'display_name' then v_display_name else display_name end,
      provider_address = case when p_fields ? 'provider_address' then v_provider_address else provider_address end
  where id = p_persona_id and user_id = p_user_id and status = 'active'
  returning * into v_persona;

  if not found then
    raise exception 'Persona not found' using errcode = 'P0002';
  end if;
  return v_persona;
end;
$$;

revoke all on function public.update_persona_addresses(uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.update_persona_addresses(uuid, uuid, jsonb) to service_role;

-- The backend validates the actual object and speaker list before marking uploaded.
-- The timestamp comes from the row read before validation, so a stale validation cannot
-- overwrite a newer selection or turn an uploaded/deleting record back into uploading.
create function public.update_record_upload(p_user_id uuid, p_record_id uuid,
  p_expected_updated_at timestamptz, p_status text, p_speakers jsonb, p_error_code text)
returns public.records
language plpgsql security invoker set search_path = ''
as $$
declare
  v_persona_id uuid;
  v_record public.records;
  v_field text;
  v_value text;
  v_target text;
  v_provider text;
begin
  if p_expected_updated_at is null or p_status is null or
    p_status not in ('uploading', 'uploaded') or
    jsonb_typeof(p_speakers) is distinct from 'object' or
    (p_error_code is not null and p_error_code not in
      ('speaker_required', 'speaker_not_found', 'speaker_not_allowed')) or
    (p_status = 'uploaded' and p_error_code is not null)
  then
    raise exception 'Invalid upload update' using errcode = '22023';
  end if;

  select persona_id into v_persona_id from public.records
    where id = p_record_id and user_id = p_user_id;
  if not found then
    raise exception 'Record not found' using errcode = 'P0002';
  end if;

  -- Match the parent-before-record lock order used by confirmation and deletion.
  perform 1 from public.personas
    where id = v_persona_id and user_id = p_user_id and status = 'active' for update;
  if not found then
    raise exception 'Record not found' using errcode = 'P0002';
  end if;

  select * into v_record from public.records
    where id = p_record_id and user_id = p_user_id and persona_id = v_persona_id for update;
  if not found or v_record.status = 'deleting' then
    raise exception 'Record not found' using errcode = 'P0002';
  end if;
  if v_record.status <> 'uploading' or v_record.updated_at is distinct from p_expected_updated_at then
    raise exception 'Upload state changed' using errcode = '55000';
  end if;

  v_target := v_record.target_speaker;
  v_provider := v_record.provider_speaker;
  for v_field in select jsonb_object_keys(p_speakers) loop
    if v_field not in ('target_speaker', 'provider_speaker') or
      jsonb_typeof(p_speakers -> v_field) not in ('string', 'null')
    then
      raise exception 'Invalid speaker patch field' using errcode = '22023';
    end if;
    v_value := regexp_replace(p_speakers ->> v_field, '^[[:space:]]+|[[:space:]]+$', '', 'g');
    if v_field = 'target_speaker' then
      v_target := v_value;
    else
      v_provider := v_value;
    end if;
  end loop;

  v_target := regexp_replace(v_target, '^[[:space:]]+|[[:space:]]+$', '', 'g');
  v_provider := regexp_replace(v_provider, '^[[:space:]]+|[[:space:]]+$', '', 'g');
  if (v_target is not null and char_length(v_target) not between 1 and 100) or
    (v_provider is not null and char_length(v_provider) not between 1 and 100)
  then
    raise exception 'Speaker must contain 1 to 100 characters' using errcode = '22023';
  end if;

  if (v_provider is not null and (v_target is null or v_target = v_provider)) or
    (v_record.kind = 'audio' and (v_target is not null or v_provider is not null))
  then
    raise exception 'Invalid final speaker selection' using errcode = '22023';
  end if;

  update public.records
    set target_speaker = v_target, provider_speaker = v_provider,
        status = p_status, error_code = p_error_code
    where id = p_record_id and user_id = p_user_id
    returning * into v_record;
  return v_record;
end;
$$;

revoke all on function public.update_record_upload(uuid, uuid, timestamptz, text, jsonb, text)
  from public, anon, authenticated;
grant execute on function public.update_record_upload(uuid, uuid, timestamptz, text, jsonb, text)
  to service_role;

notify pgrst, 'reload schema';
commit;
