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
