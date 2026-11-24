-- Run after personalization_lifecycle. All synthetic rows roll back.
begin;
set local statement_timeout='30s';
insert into auth.users(id) values
 ('a1210000-0000-4000-8000-000000000011'),('a1210000-0000-4000-8000-000000000012');
set local role service_role;
do $$
declare
 u uuid:='a1210000-0000-4000-8000-000000000011'; foreign_user uuid:='a1210000-0000-4000-8000-000000000012';
 p uuid:=gen_random_uuid(); r uuid:=gen_random_uuid(); v uuid:=gen_random_uuid();
 snapshot_id uuid:=gen_random_uuid(); op_old uuid:=gen_random_uuid(); op uuid:=gen_random_uuid();
 op_unknown uuid:=gen_random_uuid(); op_expired uuid:=gen_random_uuid();
 leaf1 uuid:=gen_random_uuid(); leaf2 uuid:=gen_random_uuid(); root uuid:=gen_random_uuid();
 duplicate_leaf uuid:=gen_random_uuid(); invalid_root uuid:=gen_random_uuid();
 request1 uuid:=gen_random_uuid(); request2 uuid:=gen_random_uuid(); request_root uuid:=gen_random_uuid();
 build1 uuid:=gen_random_uuid(); build2 uuid:=gen_random_uuid();
 chunk1 uuid:=gen_random_uuid(); chunk2 uuid:=gen_random_uuid();
 session_id uuid:=gen_random_uuid(); turn_id uuid:=gen_random_uuid(); llm_id uuid:=gen_random_uuid();
 s public.personalization_snapshots; a public.profile_analysis_attempts; n public.profile_analysis_nodes;
 profile public.style_profiles; replay public.style_profiles; turn_row public.chat_turns;
 runtime jsonb:='{"model":"test-model-r1","tokenizer":"test-tokenizer-r1","template":"test-template-r1"}';
 content jsonb:='{"schema_version":1,"speech_style":{"tone":["synthetic"],"address_terms":[],"sentence_endings":[],"frequent_expressions":[]},"conversation_habits":[],"limitations":[]}';
 range1 jsonb; range2 jsonb; chunk jsonb; context jsonb; deadline timestamptz:=clock_timestamp()+interval '10 minutes';
begin
 insert into public.personas(id,user_id,name) values(p,u,'synthetic lifecycle');
 insert into public.records(id,user_id,persona_id,kind,original_name,object_path,mime_type,status,consent_at,consent_version)
 values(r,u,p,'text','synthetic.txt',u::text||'/'||r::text||'/original','text/plain','ready',now(),'test-v1');
 insert into public.record_text_versions(id,user_id,persona_id,record_id,revision,content)
 values(v,u,p,r,1,E'alpha\nbeta');
 perform public.confirm_record_text_v2(u,v,'plain','[{"start":0,"end":5},{"start":6,"end":10}]','{}');
 begin
   update public.record_text_versions set source_units='[]' where id=v;
   raise exception 'Confirmed metadata changed in place';
 exception when check_violation then null;
 end;
 s:=public.capture_personalization_snapshot(u,p,snapshot_id,'synthetic-mask-policy-v1');
 if jsonb_array_length(s.manifest)<>2 then raise exception 'Snapshot lost source units'; end if;
 if public.capture_personalization_snapshot(u,p,snapshot_id,'synthetic-mask-policy-v1') is distinct from s
 then raise exception 'Snapshot replay changed'; end if;
 begin
   update public.personalization_snapshots set manifest='[]' where id=s.id;
   raise exception 'Snapshot changed in place';
 exception when check_violation then null;
 end;
 begin
   perform public.capture_personalization_snapshot(foreign_user,p,gen_random_uuid(),'synthetic');
   raise exception 'Foreign owner captured source data';
 exception when sqlstate 'P0002' then null;
 end;
 range1:=jsonb_build_array(jsonb_build_object('unit_id',s.manifest->0->>'id','start',0,'end',5));
 range2:=jsonb_build_array(jsonb_build_object('unit_id',s.manifest->1->>'id','start',0,'end',4));
 a:=public.start_profile_analysis(u,s.id,op_old,deadline,runtime);
 perform public.start_profile_analysis_node(u,op_old,a.attempt,leaf1,request1,'analyze',range1,'{}');
 a:=public.start_profile_analysis(u,s.id,op,deadline,runtime);
 if a.attempt<>2 then raise exception 'Analysis attempt did not advance'; end if;
 begin
   perform public.finish_profile_analysis_node(u,op_old,1,leaf1,request1,runtime,'done',content,'test-model',null);
   raise exception 'Old attempt saved a late result';
 exception when sqlstate '55000' then null;
 end;
 if public.get_profile_analysis_result(u,op_old,1)->>'publication_state'<>'not_committed'
 then raise exception 'Superseded attempt outcome is ambiguous'; end if;
 if public.get_profile_analysis_result(u,op,2)->>'publication_state'<>'unresolved'
 then raise exception 'Running operation was declared rolled back'; end if;

 request1:=gen_random_uuid();
 perform public.start_profile_analysis_node(u,op,2,leaf1,request1,'analyze',range1,'{}');
 perform public.start_profile_analysis_node(u,op,2,leaf2,request2,'analyze',range2,'{}');
 begin
   perform public.finish_profile_analysis_node(u,op,2,leaf1,request1,
     runtime||'{"model":"different-revision"}','done',content,'test-model',null);
   raise exception 'Mixed runtime result was accepted';
 exception when sqlstate '22023' then null;
 end;
 perform public.finish_profile_analysis_node(u,op,2,leaf1,request1,runtime,'done',content,'test-model',null);
 perform public.finish_profile_analysis_node(u,op,2,leaf2,request2,runtime,'done',content,'test-model',null);
 begin
   perform public.publish_analysis_profile(u,op,2,leaf1);
   raise exception 'Partial coverage was published';
 exception when sqlstate '55000' then null;
 end;
 perform public.start_profile_analysis_node(u,op,2,duplicate_leaf,gen_random_uuid(),'analyze',range1,'{}');
 select * into n from public.profile_analysis_nodes where operation_id=op and node_id=duplicate_leaf;
 perform public.finish_profile_analysis_node(u,op,2,n.node_id,n.request_id,runtime,'done',content,'test-model',null);
 n:=public.start_profile_analysis_node(u,op,2,invalid_root,gen_random_uuid(),'merge','[]',array[leaf1,duplicate_leaf]);
 perform public.finish_profile_analysis_node(u,op,2,n.node_id,n.request_id,runtime,'done',content,'test-model',null);
 begin
   perform public.publish_analysis_profile(u,op,2,invalid_root);
   raise exception 'Duplicate input coverage was published';
 exception when sqlstate '55000' then null;
 end;
 perform public.start_profile_analysis_node(u,op,2,root,request_root,'merge','[]',array[leaf1,leaf2]);
 perform public.finish_profile_analysis_node(u,op,2,root,request_root,runtime,'done',content,'test-model',null);
 begin
   perform public.publish_style_profile(u,p,s.source_revision,content,'test-model',array[v]);
   raise exception 'Legacy publication bypassed the current-attempt guard';
 exception when sqlstate '55000' then null;
 end;
 profile:=public.publish_analysis_profile(u,op,2,root);
 set constraints all immediate;
 set constraints all deferred;
 replay:=public.publish_analysis_profile(u,op,2,root);
 if replay.id<>profile.id or (select count(*) from public.style_profiles where persona_id=p)<>1
 then raise exception 'Publication replay created another version'; end if;
 if public.get_profile_analysis_result(u,op,2)->>'publication_state'<>'committed'
 then raise exception 'Publication receipt lost'; end if;
 if public.get_record_reflection(u,r)<>'pending' then raise exception 'Profile alone was called reflected'; end if;

 perform public.start_record_index_build(u,s.id,build1,'test-encoder','r1',2);
 chunk:=jsonb_build_object('id',chunk1,'chunk_no',0,'content','alpha','coverage',range1,'embedding','[1,0]'::jsonb);
 perform public.append_record_index_chunks(u,build1,jsonb_build_array(chunk));
 perform public.append_record_index_chunks(u,build1,jsonb_build_array(chunk));
 begin
   perform public.activate_record_index_build(u,build1);
   raise exception 'Partial index activated';
 exception when sqlstate '55000' then null;
 end;
 begin
   perform public.append_record_index_chunks(u,build1,jsonb_build_array(
     jsonb_build_object('id',chunk2,'chunk_no',1,'content','beta','coverage',range2,'embedding','[1]'::jsonb)));
   raise exception 'Encoder dimension mismatch accepted';
 exception when sqlstate '22023' then null;
 end;
 perform public.append_record_index_chunks(u,build1,jsonb_build_array(
   jsonb_build_object('id',chunk2,'chunk_no',1,'content','beta','coverage',range2,'embedding','[0,1]'::jsonb)));
 perform public.activate_record_index_build(u,build1);
 if public.get_record_reflection(u,r)<>'reflected' then raise exception 'Shared ready snapshot not reflected'; end if;
 context:=public.read_persona_context(u,p,'test-encoder','r1','[1,0]'::extensions.vector,10,-1);
 if context->>'source_stamp'<>s.source_stamp::text or jsonb_array_length(context->'evidence')<>2
 then raise exception 'Context mixed source stamps'; end if;
 begin
   perform public.read_persona_context(foreign_user,p,'test-encoder','r1','[1,0]'::extensions.vector,10,-1);
   raise exception 'Foreign owner read context';
 exception when sqlstate 'P0002' then null;
 end;
 perform public.start_record_index_build(u,s.id,build2,'test-encoder','r2',2);
 begin
   perform public.activate_record_index_build(u,build2);
   raise exception 'Empty replacement index activated';
 exception when sqlstate '55000' then null;
 end;
 if not exists(select from public.record_index_builds where id=build1 and status='ready')
 then raise exception 'Failed replacement retired the valid build'; end if;
 perform public.fail_record_index_build(u,build2);

 a:=public.start_profile_analysis(u,s.id,op_unknown,deadline,runtime);
 n:=public.start_profile_analysis_node(u,op_unknown,a.attempt,gen_random_uuid(),gen_random_uuid(),'analyze',range1,'{}');
 perform public.finish_profile_analysis_node(u,op_unknown,a.attempt,n.node_id,n.request_id,runtime,'unknown',null,null,'timeout');
 if public.get_profile_analysis_result(u,op_unknown,a.attempt)->>'publication_state'<>'not_committed'
 then raise exception 'Terminal unknown node did not seal publication'; end if;
 a:=public.start_profile_analysis(u,s.id,op_expired,deadline,runtime);
 update public.profile_analysis_attempts set deadline=clock_timestamp()-interval '1 second' where operation_id=op_expired;
 begin
   perform public.start_profile_analysis_node(u,op_expired,a.attempt,gen_random_uuid(),gen_random_uuid(),'analyze',range1,'{}');
   raise exception 'Expired attempt accepted a node';
 exception when sqlstate '55000' then null;
 end;

 insert into public.chat_sessions(id,user_id,persona_id,ai_notice_ack_at) values(session_id,u,p,now());
 perform public.create_chat_turn(u,session_id,turn_id,'text','synthetic question');
 turn_row:=public.start_chat_stage_v2(u,turn_id,'llm','test-model',false,llm_id);
 perform public.bind_persona_chat_turn(u,turn_id,llm_id,s.id,build1,profile.id);
 perform public.check_persona_chat_turn(u,turn_id,llm_id,turn_row.llm_attempt);
 perform set_config('replica.test.profile',profile.id::text,true);
 perform set_config('replica.test.persona',p::text,true);
 perform set_config('replica.test.record',r::text,true);
 perform set_config('replica.test.snapshot',s.id::text,true);
 perform set_config('replica.test.turn',turn_id::text,true);
 perform set_config('replica.test.request',llm_id::text,true);
 perform set_config('replica.test.operation',op::text,true);
end;
$$;

-- Deleting the context binding must never reopen the legacy completion path.
do $$
declare
 u uuid:='a1210000-0000-4000-8000-000000000011';
 turn_id uuid:=current_setting('replica.test.turn')::uuid;
 request_id uuid:=current_setting('replica.test.request')::uuid;
begin
 begin
   delete from public.style_profiles where id=current_setting('replica.test.profile')::uuid;
   begin
     perform public.finish_chat_stage_v2(u,turn_id,'llm',1,true,'unbound late answer',1,null,1,'stop',request_id);
     raise exception 'Deleted binding bypassed completion guard';
   exception when sqlstate 'P0002' then null;
   end;
   raise exception 'Restore fixture after deletion check' using errcode='P1299';
 exception when sqlstate 'P1299' then null;
 end;
end $$;

do $$
declare
 u uuid:='a1210000-0000-4000-8000-000000000011';
 p uuid:=gen_random_uuid(); r uuid:=gen_random_uuid(); v uuid:=gen_random_uuid(); next_v uuid:=gen_random_uuid();
 unit_set jsonb:='[{"start":0,"end":3,"speaker":"대상"},{"start":4,"end":7,"speaker":"제공자"}]';
 updated_set jsonb:='[{"start":0,"end":3,"speaker":"대상"},{"start":4,"end":7,"speaker":"새제공자"}]';
begin
 insert into public.personas(id,user_id,name) values(p,u,'synthetic speaker inheritance');
 insert into public.records(id,user_id,persona_id,kind,original_name,object_path,mime_type,status,consent_at,
   consent_version,target_speaker,provider_speaker)
 values(r,u,p,'text','synthetic.txt',u::text||'/'||r::text||'/original','text/plain','ready',now(),'test','대상','제공자');
 insert into public.record_text_versions(id,user_id,persona_id,record_id,revision,content)
 values(v,u,p,r,1,E'foo\nbar');
 perform public.confirm_record_text_v2(u,v,'kakao',unit_set,'{}');
 insert into public.record_text_versions(id,user_id,persona_id,record_id,revision,content)
 values(next_v,u,p,r,2,E'foo\nbar');
 begin
   perform public.confirm_record_text_v2(u,next_v,'kakao',updated_set,'{}');
   raise exception 'Changed speaker set inherited confirmation';
 exception when sqlstate 'P1202' then null;
 end;
 if (select status from public.record_text_versions where id=v)<>'confirmed'
   or (select status from public.record_text_versions where id=next_v)<>'draft'
 then raise exception 'Rejected confirmation damaged the previous revision'; end if;
 begin
   perform public.confirm_record_text_v2(u,next_v,'plain','[{"start":0,"end":7}]',
     '{"target_speaker":null,"provider_speaker":null}');
   raise exception 'A Kakao edit dropped its speaker format';
 exception when sqlstate 'P1202' then null;
 end;
 perform public.confirm_record_text_v2(u,next_v,'kakao',updated_set,'{"provider_speaker":"새제공자"}');
 if (select status from public.record_text_versions where id=next_v)<>'confirmed'
   or (select status from public.record_text_versions where id=v)<>'superseded'
 then raise exception 'Explicit speaker reconfirmation failed'; end if;
end $$;

set local role authenticated;
select set_config('request.jwt.claim.sub','a1210000-0000-4000-8000-000000000012',true);
do $$ begin
 if exists(select from public.personalization_snapshots) or exists(select from public.profile_analysis_nodes)
 then raise exception 'RLS exposed another owner lifecycle'; end if;
end $$;
select set_config('request.jwt.claim.sub','a1210000-0000-4000-8000-000000000011',true);
do $$ begin
 if not exists(select from public.personalization_snapshots) or not exists(select from public.record_index_chunks)
 then raise exception 'Owner cannot read the current snapshot'; end if;
end $$;

set local role service_role;
do $$
declare
 u uuid:='a1210000-0000-4000-8000-000000000011';
 r uuid:=current_setting('replica.test.record')::uuid;
 turn_id uuid:=current_setting('replica.test.turn')::uuid;
 request_id uuid:=current_setting('replica.test.request')::uuid;
 snapshot_id uuid:=current_setting('replica.test.snapshot')::uuid;
 op uuid:=current_setting('replica.test.operation')::uuid;
 p uuid:=current_setting('replica.test.persona')::uuid;
 old_stamp uuid; t public.chat_turns;
begin
 select source_stamp into old_stamp from public.personas where id=p;
 update public.records set consent_version='test-v2' where id=r;
 if exists(select from public.personas where id=p and source_stamp=old_stamp)
 then raise exception 'Consent change did not advance the source stamp'; end if;
 begin
   perform public.check_persona_chat_turn(u,turn_id,request_id,1);
   raise exception 'Stale context passed before AI call';
 exception when sqlstate '55000' then null;
 end;
 begin
   perform public.finish_chat_stage_v2(u,turn_id,'llm',1,true,'late answer',1,null,1,'stop',request_id);
   raise exception 'Existing completion path bypassed source validation';
 exception when sqlstate '55000' then null;
 end;
 if (select answer_status from public.chat_turns where id=turn_id)<>'pending'
 then raise exception 'Rejected stale answer partially changed the turn'; end if;
 t:=public.finish_persona_chat_turn(u,turn_id,request_id,1,false,null,1,'source_changed',1,null);
 if t.answer_status<>'failed' then raise exception 'A stale attempt could not record failure'; end if;
 if public.get_profile_analysis_result(u,op,2)->>'publication_state'<>'committed'
 then raise exception 'Source invalidation erased a committed receipt'; end if;
 begin
   perform public.start_profile_analysis(u,snapshot_id,gen_random_uuid(),clock_timestamp()+interval '1 minute',
     '{"model":"r","tokenizer":"r","template":"r"}');
   raise exception 'Stale snapshot started another analysis';
 exception when sqlstate '55000' then null;
 end;
 update public.records set status='deleting' where id=r;
end $$;

set local role authenticated;
do $$ begin
 if exists(select from public.personalization_snapshots) or exists(select from public.record_index_chunks)
 then raise exception 'Invalidated sources still visible through RLS'; end if;
end $$;
reset role;
do $$
declare f record;
begin
 for f in select p.oid from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname in ('capture_personalization_snapshot','start_profile_analysis',
 'start_profile_analysis_node','finish_profile_analysis_node','publish_analysis_profile',
 'read_persona_context','bind_persona_chat_turn','check_persona_chat_turn','finish_persona_chat_turn') loop
   if has_function_privilege('anon',f.oid,'execute') or has_function_privilege('authenticated',f.oid,'execute')
     or not has_function_privilege('service_role',f.oid,'execute')
   then raise exception 'Lifecycle RPC is not server-only'; end if;
 end loop;
end $$;
rollback;
