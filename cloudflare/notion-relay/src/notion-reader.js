import {ProtocolError, requireValue, uuid, SOURCE_ID, REPOSITORY_ID, sha256} from './protocol.js';

const DISPLAY_STATES = new Set(['백로그','준비 중','진행 중','검토 중','완료','취소']);
function normalizedID(value) {
  requireValue(typeof value === 'string' && value.length <= 256, 'PAGE_PROPERTY_BINDING');
  try { return decodeURIComponent(value); }
  catch { throw new ProtocolError('PAGE_PROPERTY_BINDING',409); }
}
function text(property) {
  requireValue(property?.type === 'rich_text' && Array.isArray(property.rich_text) &&
    property.rich_text.length <= 100, 'PAGE_TEXT_SHAPE');
  const pieces = property.rich_text.map(part => {
    requireValue(part.type === 'text' && typeof part.text?.content === 'string' &&
      (part.plain_text === undefined || part.plain_text === part.text.content), 'PAGE_TEXT_SHAPE');
    return part.text.content;
  });
  const raw = pieces.join('');
  requireValue(raw.length <= 18000, 'PAGE_TEXT_LIMIT');
  return raw;
}
function selection(property) {
  requireValue(property?.type === 'select' &&
    (property.select === null || typeof property.select?.name === 'string'), 'PAGE_SELECT_SHAPE');
  return property.select?.name ?? null;
}
export async function currentPage(page, pageID, env) {
  requireValue(page?.object === 'page' && uuid(page.id) === uuid(pageID), 'PAGE_IDENTITY');
  requireValue(page.parent?.type === 'data_source_id' &&
    page.parent.data_source_id === SOURCE_ID, 'PAGE_SOURCE');
  requireValue(page.archived === false && page.in_trash === false, 'PAGE_ARCHIVED');
  const names = {'종류':'NOTION_KIND_PROPERTY_ID','동기화 키':'NOTION_KEY_PROPERTY_ID',
    '작업 상태':'NOTION_TASK_PROPERTY_ID','동기화 내부 상태':'NOTION_STATE_PROPERTY_ID'};
  requireValue(page.properties && typeof page.properties === 'object', 'PAGE_PROPERTIES');
  for (const [name, variable] of Object.entries(names)) {
    requireValue(normalizedID(page.properties[name]?.id) === normalizedID(env[variable]),
      'PAGE_PROPERTY_BINDING');
  }
  requireValue(selection(page.properties['종류']) === 'Issue', 'PAGE_KIND');
  const key = text(page.properties['동기화 키']);
  const match = /^gh:1392442366:issue:([1-9][0-9]*)$/.exec(key);
  requireValue(match && Number.isSafeInteger(Number(match[1])), 'PAGE_KEY');
  const status = selection(page.properties['작업 상태']);
  requireValue(status === null || DISPLAY_STATES.has(status), 'PAGE_STATUS');
  const state = text(page.properties['동기화 내부 상태']);
  // This is comparison-only. Only Python's existing exact decoder can produce
  // a handled checkpoint/reflection proof; an arbitrary latest page cannot.
  return {page_id:pageID, key, repository_id:REPOSITORY_ID, issue_id:Number(match[1]),
    status, state_sha256:await sha256(state)};
}
export async function readCurrentPage(pageID, env, fetcher = fetch) {
  uuid(pageID);
  requireValue(typeof env.NOTION_READ_TOKEN === 'string' && env.NOTION_READ_TOKEN.length >= 20,
    'NOTION_READ_UNCONFIGURED',503);
  let response;
  try {
    response = await fetcher(`https://api.notion.com/v1/pages/${pageID}`, {
      method:'GET',redirect:'manual',signal:AbortSignal.timeout(10000),headers:{
        'Authorization':`Bearer ${env.NOTION_READ_TOKEN}`,'Notion-Version':'2026-03-11'}});
  } catch { throw new ProtocolError('NOTION_READ_UNKNOWN',503); }
  requireValue(response.status === 200, 'NOTION_READ_UNAVAILABLE',503);
  const reader = response.body?.getReader();
  requireValue(reader,'NOTION_RESPONSE_BODY',503);
  const parts=[];let size=0;
  try {
    for (;;) {
      const {done,value}=await reader.read();if(done)break;
      size+=value.length;
      if(size>262144){await reader.cancel();throw new ProtocolError('NOTION_RESPONSE_LIMIT',503);}
      parts.push(value);
    }
  } finally {reader.releaseLock();}
  const bytes=new Uint8Array(size);let offset=0;
  for(const value of parts){bytes.set(value,offset);offset+=value.length;}
  let page;
  try {page=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}
  catch {throw new ProtocolError('NOTION_RESPONSE_SHAPE',503);}
  return currentPage(page,pageID,env);
}

export function matchesCheckpoint(latest, checkpoint) {
  return checkpoint && ['page_id','key','repository_id','status','state_sha256']
    .every(name => latest[name] === checkpoint[name]);
}
