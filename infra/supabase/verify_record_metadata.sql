-- Run after record_metadata.sql. Synthetic records and users are rolled back together.
begin;
set local statement_timeout = '20s';

insert into auth.users(id) values
  ('a1210000-0000-4000-8000-000000000001'),
  ('a1210000-0000-4000-8000-000000000002');

insert into public.personas(id, user_id, name, data_revision)
values ('b1210000-0000-4000-8000-000000000001', 'a1210000-0000-4000-8000-000000000001',
        'Legacy persona name is preserved', 7);

insert into public.records(id, user_id, persona_id, kind, original_name, object_path, mime_type, target_speaker)
values ('c1210000-0000-4000-8000-000000000001', 'a1210000-0000-4000-8000-000000000001',
        'b1210000-0000-4000-8000-000000000001', 'text', 'synthetic.txt',
        'a1210000-0000-4000-8000-000000000001/c1210000-0000-4000-8000-000000000001/original',
        'text/plain', 'Legacy target');

set local role service_role;
do $$
declare
  u constant uuid := 'a1210000-0000-4000-8000-000000000001';
  other_user constant uuid := 'a1210000-0000-4000-8000-000000000002';
  p constant uuid := 'b1210000-0000-4000-8000-000000000001';
  r constant uuid := 'c1210000-0000-4000-8000-000000000001';
  saved public.personas;
  patch jsonb;
begin
  select * into strict saved from public.personas where id = p;
  if saved.display_name is not null or saved.provider_address is not null or not exists (
    select from public.records where id = r and target_speaker = 'Legacy target' and provider_speaker is null
  ) then raise exception 'Legacy inserts no longer work'; end if;

  saved := public.update_persona_addresses(u, p, '{"display_name":"  Friend  ","provider_address":"\tCaller\n"}');
  if saved.display_name is distinct from 'Friend' or saved.provider_address is distinct from 'Caller'
  then raise exception 'Address values were not normalized'; end if;

  saved := public.update_persona_addresses(u, p, jsonb_build_object('display_name', repeat('가', 50)));
  if saved.display_name is distinct from repeat('가', 50) or saved.provider_address is distinct from 'Caller'
  then raise exception 'Character limit or omitted key handling failed'; end if;

  saved := public.update_persona_addresses(u, p, '{"provider_address":null}');
  if saved.provider_address is not null or saved.display_name is distinct from repeat('가', 50)
    or saved.name is distinct from 'Legacy persona name is preserved' or saved.data_revision is distinct from 7
  then raise exception 'Explicit null, original name or data revision changed unexpectedly'; end if;

  foreach patch in array array[
    null::jsonb, 'null'::jsonb, '[]'::jsonb, '{}'::jsonb, '{"name":"bad"}'::jsonb,
    '{"display_name":4}'::jsonb, '{"provider_address":" \t\n "}'::jsonb,
    jsonb_build_object('display_name', repeat('가', 51))
  ] loop
    begin
      perform public.update_persona_addresses(u, p, patch);
      raise exception 'Accepted an invalid address patch';
    exception when sqlstate '22023' then null;
    end;
  end loop;

  begin
    perform public.update_persona_addresses(other_user, p, '{"display_name":"Other"}');
    raise exception 'A different owner changed the address';
  exception when sqlstate 'P0002' then null;
  end;

  update public.records set provider_speaker = repeat('가', 100) where id = r;
  begin
    update public.records set provider_speaker = repeat('가', 101) where id = r;
    raise exception 'An oversized speaker label was accepted';
  exception when check_violation then null;
  end;
  begin
    update public.records set provider_speaker = E' Caller\t' where id = r;
    raise exception 'An untrimmed speaker label was accepted';
  exception when check_violation then null;
  end;

  update public.personas set status = 'deleting' where id = p;
  begin
    perform public.update_persona_addresses(u, p, '{"display_name":"Deleting"}');
    raise exception 'A deleting persona accepted an edit';
  exception when sqlstate 'P0002' then null;
  end;
  update public.personas set status = 'active' where id = p;
end;
$$;

do $$
declare
  u constant uuid := 'a1210000-0000-4000-8000-000000000001';
  other_user constant uuid := 'a1210000-0000-4000-8000-000000000002';
  p constant uuid := 'b1210000-0000-4000-8000-000000000001';
  r constant uuid := 'c1210000-0000-4000-8000-000000000001';
  saved public.records;
  previous_time timestamptz;
  source_revision bigint;
  patch jsonb;
  next_error text;
begin
  select * into strict saved from public.records where id = r;
  select data_revision into source_revision from public.personas where id = p;
  previous_time := saved.updated_at;
  saved := public.update_record_upload(u, r, saved.updated_at, 'uploading',
    '{"target_speaker":"  Target  ","provider_speaker":"\tProvider\n"}', null);
  if saved.target_speaker is distinct from 'Target' or saved.provider_speaker is distinct from 'Provider'
  then raise exception 'Speaker values were not normalized'; end if;

  foreach next_error in array array['speaker_required', 'speaker_not_found', 'speaker_not_allowed'] loop
    saved := public.update_record_upload(u, r, saved.updated_at, 'uploading', '{}', next_error);
    if saved.status is distinct from 'uploading' or saved.error_code is distinct from next_error
      or saved.target_speaker is distinct from 'Target' or saved.provider_speaker is distinct from 'Provider'
    then raise exception 'Speaker error did not preserve the upload and selection'; end if;
  end loop;
  if exists (select from public.personas where id = p and data_revision <> source_revision)
  then raise exception 'Upload validation changed the confirmed-input revision'; end if;

  begin
    perform public.update_record_upload(u, r, previous_time, 'uploading', '{}', 'speaker_required');
    raise exception 'An earlier validation overwrote the latest selection';
  exception when sqlstate '55000' then null;
  end;
  begin
    perform public.update_record_upload(other_user, r, saved.updated_at, 'uploading', '{}', null);
    raise exception 'Another owner updated an upload';
  exception when sqlstate 'P0002' then null;
  end;

  foreach patch in array array[
    null::jsonb, 'null'::jsonb, '[]'::jsonb, '{"unexpected":"x"}'::jsonb,
    '{"target_speaker":4}'::jsonb, '{"target_speaker":" \t\n "}'::jsonb,
    '{"target_speaker":null}'::jsonb, '{"provider_speaker":"Target"}'::jsonb,
    jsonb_build_object('target_speaker', repeat('가', 101))
  ] loop
    begin
      perform public.update_record_upload(u, r, saved.updated_at, 'uploading', patch, null);
      raise exception 'An invalid speaker patch was accepted';
    exception when sqlstate '22023' then null;
    end;
  end loop;
  begin
    perform public.update_record_upload(u, r, null, 'uploading', '{}', null);
    raise exception 'A missing modification time was accepted';
  exception when sqlstate '22023' then null;
  end;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'ready', '{}', null);
    raise exception 'The upload RPC accepted an unrelated transition';
  exception when sqlstate '22023' then null;
  end;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'uploaded', '{}', 'speaker_required');
    raise exception 'A completed upload kept a speaker error';
  exception when sqlstate '22023' then null;
  end;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'uploading', '{}', 'untrusted-error');
    raise exception 'An arbitrary upload error was accepted';
  exception when sqlstate '22023' then null;
  end;

  -- Explicit null clears a selection and omitted fields keep their values.
  saved := public.update_record_upload(u, r, saved.updated_at, 'uploading', '{"provider_speaker":null}', null);
  if saved.target_speaker is distinct from 'Target' or saved.provider_speaker is not null
    or saved.error_code is not null
  then raise exception 'Speaker patch null/omission handling failed'; end if;

  saved := public.update_record_upload(u, r, saved.updated_at, 'uploading', '{"target_speaker":null}', null);
  if saved.target_speaker is not null or saved.provider_speaker is not null
  then raise exception 'Clearing both selections failed'; end if;

  -- Audio and inactive parents reject speaker changes even for the server role.
  update public.records set kind = 'audio', mime_type = 'audio/wav' where id = r returning * into saved;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'uploading', '{"target_speaker":"Target"}', null);
    raise exception 'An audio upload accepted a text speaker';
  exception when sqlstate '22023' then null;
  end;
  update public.records set kind = 'text', mime_type = 'text/plain' where id = r returning * into saved;
  update public.personas set status = 'deleting' where id = p;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'uploading', '{}', null);
    raise exception 'A deleting parent accepted an upload update';
  exception when sqlstate 'P0002' then null;
  end;
  update public.personas set status = 'active' where id = p;

  -- A successful upload saves the final selection and clears the persisted warning.
  saved := public.update_record_upload(u, r, saved.updated_at, 'uploading', '{}', 'speaker_required');
  previous_time := saved.updated_at;
  saved := public.update_record_upload(u, r, saved.updated_at, 'uploaded', '{"target_speaker":"Target"}', null);
  if saved.status is distinct from 'uploaded' or saved.target_speaker is distinct from 'Target'
    or saved.error_code is not null
  then raise exception 'Upload completion was not stored atomically'; end if;
  begin
    perform public.update_record_upload(u, r, previous_time, 'uploading', '{}', 'speaker_required');
    raise exception 'A late validation reopened the completed upload';
  exception when sqlstate '55000' then null;
  end;

  update public.records set status = 'deleting' where id = r returning * into saved;
  begin
    perform public.update_record_upload(u, r, saved.updated_at, 'uploading', '{}', null);
    raise exception 'A deleting record was reopened';
  exception when sqlstate 'P0002' then null;
  end;

  -- Preserve the fixtures used by the following owner-read checks.
  update public.records set status = 'uploading', target_speaker = 'Legacy target',
    provider_speaker = repeat('가', 100), error_code = null where id = r;
end;
$$;

reset role;
do $$
declare
  rpc regprocedure := 'public.update_persona_addresses(uuid,uuid,jsonb)'::regprocedure;
begin
  if has_function_privilege('anon', rpc, 'EXECUTE') or
    has_function_privilege('authenticated', rpc, 'EXECUTE') or
    not has_function_privilege('service_role', rpc, 'EXECUTE') or
    (select prosecdef from pg_proc where oid = rpc)
  then raise exception 'Address RPC privileges are incorrect'; end if;

  rpc := 'public.update_record_upload(uuid,uuid,timestamptz,text,jsonb,text)'::regprocedure;
  if has_function_privilege('anon', rpc, 'EXECUTE') or
    has_function_privilege('authenticated', rpc, 'EXECUTE') or
    not has_function_privilege('service_role', rpc, 'EXECUTE') or
    (select prosecdef from pg_proc where oid = rpc)
  then raise exception 'Upload RPC privileges are incorrect'; end if;
end;
$$;

set local role authenticated;
select set_config('request.jwt.claim.sub', 'a1210000-0000-4000-8000-000000000002', true);
do $$
begin
  if exists (select from public.personas where id = 'b1210000-0000-4000-8000-000000000001') or
    exists (select from public.records where id = 'c1210000-0000-4000-8000-000000000001')
  then raise exception 'Another owner can read the new metadata'; end if;
end;
$$;

select set_config('request.jwt.claim.sub', 'a1210000-0000-4000-8000-000000000001', true);
do $$
begin
  if not exists (select from public.personas
    where id = 'b1210000-0000-4000-8000-000000000001' and display_name = repeat('가', 50)) or
    not exists (select from public.records
      where id = 'c1210000-0000-4000-8000-000000000001' and provider_speaker = repeat('가', 100))
  then raise exception 'The owner cannot read the new metadata'; end if;
end;
$$;

rollback;
