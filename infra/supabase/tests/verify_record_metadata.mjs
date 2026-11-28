import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
if (!process.argv[2]) throw new Error("Pass the installed PGlite module path");
const { PGlite } = await import(pathToFileURL(resolve(process.argv[2])).href);

const readSql = (name) => readFile(new URL("../" + name, import.meta.url), "utf8");
const redesign = (await readSql("redesign.sql")).replaceAll("\r\n", "\n");
const source = (first, last) => redesign.slice(redesign.indexOf(first), redesign.indexOf(last));
const db = new PGlite();
let phase = "baseline";
try {
  await db.exec(`
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create schema auth;
    create schema replica_private;
    create table auth.users(id uuid primary key);
    create function auth.uid() returns uuid language sql stable
      as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
    grant usage on schema auth to authenticated;
    create table public.personas(
      id uuid primary key, user_id uuid not null references auth.users, name text not null,
      unique (id, user_id)
    );
    create table public.style_profiles(id uuid primary key, persona_id uuid, user_id uuid, status text);
  `);
  await db.exec(source("alter table public.personas", "alter table public.chat_sessions"));
  await db.exec(source("create table public.records", "create table public.record_text_versions"));
  await db.exec(source("create function replica_private.touch_updated_at", "do $$\ndeclare t text;"));
  await db.exec(source("create function replica_private.invalidate_personalization", "create trigger invalidate_record_text"));
  await db.exec(source("create trigger invalidate_record after", "create function replica_private.guard_text_version"));
  await db.exec(`
    create trigger touch_updated_at before update on public.personas for each row
      execute function replica_private.touch_updated_at();
    create trigger touch_updated_at before update on public.records for each row execute function replica_private.touch_updated_at();
    alter table public.personas enable row level security;
    alter table public.records enable row level security;
    grant select on public.personas, public.records to authenticated;
    grant select, update on public.personas, public.records, public.style_profiles to service_role;
    ${source("create policy personas_owner_read", "create policy chat_sessions_owner_read")}
    ${source("create policy records_owner_read", "create policy record_text_owner_read")}
    insert into auth.users values ('d1210000-0000-4000-8000-000000000001');
    insert into public.personas(id,user_id,name,data_revision)
      values ('d1210000-0000-4000-8000-000000000002','d1210000-0000-4000-8000-000000000001','previous name',8);
    insert into public.records(id,user_id,persona_id,kind,original_name,object_path,mime_type,target_speaker)
      values ('d1210000-0000-4000-8000-000000000003','d1210000-0000-4000-8000-000000000001',
        'd1210000-0000-4000-8000-000000000002','text','old.txt',
        'd1210000-0000-4000-8000-000000000001/d1210000-0000-4000-8000-000000000003/original',
        'text/plain','previous speaker');
  `);
  phase = "apply actual patch";
  await db.exec(await readSql("record_metadata.sql"));
  phase = "existing data";
  const { rows } = await db.query(`
    select p.name,p.data_revision,p.display_name,p.provider_address,r.target_speaker,r.provider_speaker
    from public.personas p join public.records r on r.persona_id=p.id
  `);
  assert.deepEqual(rows, [{name:"previous name",data_revision:8,display_name:null,provider_address:null,
    target_speaker:"previous speaker",provider_speaker:null}]);
  phase = "unchanged rollback verification against source policies";
  await db.exec(await readSql("verify_record_metadata.sql"));
  assert.equal((await db.query("select count(*)::int as n from public.personas")).rows[0].n,1);
  assert.equal((await db.query("select count(*)::int as n from auth.users")).rows[0].n,1);
  console.log("PostgreSQL " + (await db.query("show server_version")).rows[0].server_version + ": patch, legacy rows, service_role RPC, RLS, and rollback passed.");
} catch (err) {
  console.error({phase,message:err.message,code:err.code,detail:err.detail,where:err.where});
  process.exitCode=1;
} finally {
  await db.close();
}