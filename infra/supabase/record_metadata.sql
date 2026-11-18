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

notify pgrst, 'reload schema';
commit;
