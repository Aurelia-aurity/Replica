import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
if (!process.argv[2]) throw new Error("Pass the installed PGlite module path");
const moduleUrl = pathToFileURL(resolve(process.argv[2]));
const { PGlite } = await import(moduleUrl.href);
const { vector } = await import(new URL("./vector/index.js", moduleUrl).href);
const db = new PGlite({ extensions: { vector } });
const sql = name => readFile(new URL("../" + name, import.meta.url), "utf8");
let phase = "baseline";
try {
  await db.exec(`
    create role anon; create role authenticated; create role service_role bypassrls;
    create schema auth; create schema storage; create schema extensions;
    create table auth.users(id uuid primary key);
    create function auth.uid() returns uuid language sql stable
      as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    grant usage on schema auth to authenticated;
    create table storage.objects(id uuid primary key default gen_random_uuid(),bucket_id text,name text);
    create table public.personas(id uuid primary key default gen_random_uuid(),
      user_id uuid not null references auth.users,name text not null,created_at timestamptz default now(),
      unique(id,user_id));
    create table public.chat_sessions(id uuid primary key default gen_random_uuid(),
      user_id uuid not null references auth.users,persona_id uuid not null,ai_notice_ack_at timestamptz,
      created_at timestamptz default now(),unique(id,user_id),
      foreign key(persona_id,user_id) references public.personas(id,user_id));
    create table public.chat_turns(id uuid primary key default gen_random_uuid(),
      user_id uuid not null references auth.users,session_id uuid not null,input_mode text not null,
      question text,question_status text default 'pending',answer text,answer_status text,tts_status text,
      model_name text,stt_ms integer,llm_ms integer,tts_ms integer,total_ms integer,error_message text,
      created_at timestamptz default now(),unique(id,user_id),
      foreign key(session_id,user_id) references public.chat_sessions(id,user_id) on delete cascade,
      constraint chat_turns_tts_after_answer check(tts_status is null or answer_status='done'));
    create function extensions.digest(text,text) returns bytea language sql immutable
      as 'select pg_catalog.sha256(pg_catalog.convert_to($1,''UTF8''))';
  `);
  await db.exec(await sql("redesign.sql"));
  await db.exec(await sql("weekend_integration.sql"));
  await db.exec(await sql("guard_llm_request_id.sql"));
  await db.exec(await sql("expire_chat_stages_v2.sql"));
  await db.exec(await sql("record_metadata.sql"));
  phase = "lifecycle patch";
  await db.exec(await sql("20261124043921_personalization_lifecycle.sql"));
  await db.exec(await sql("20261124050058_personalization_lifecycle_followup.sql"));
  phase = "rollback lifecycle verification";
  await db.exec(await sql("verify_personalization_lifecycle.sql"));
  assert.equal((await db.query("select count(*)::int as n from auth.users")).rows[0].n,0);
  console.log("PostgreSQL " + (await db.query("show server_version")).rows[0].server_version +
    ": complete source schema, lifecycle patch and rollback verification passed.");
} catch (error) {
  console.error({phase,message:error.message,code:error.code,detail:error.detail,where:error.where});
  process.exitCode=1;
} finally {
  await db.close();
}
