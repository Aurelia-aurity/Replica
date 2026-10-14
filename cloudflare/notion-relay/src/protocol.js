// Fixed repository/resource identities. Credentials never appear in this module.
export const REPOSITORY_ID = 1392442366;
export const PROJECT_ID = 'PVT_kwHOBda_2c4BmKKV';
export const STATUS_FIELD_ID = 'PVTSSF_lAHOBda_2c4BmKKVzhk0RA4';
export const SOURCE_ID = '398c7ff6-9c66-43de-ba4d-3f2670b25a87';
export const GENERAL_OPTIONS = Object.freeze({
  '백로그': 'f75ad846', '준비 중': '96beb7e0', '진행 중': '47fc9ee4',
});
export const MAX_BODY = 65536;
export class ProtocolError extends Error {
  constructor(code, status = 409) { super(code); this.code = code; this.status = status; }
}
export function requireValue(condition, code, status = 400) {
  if (!condition) throw new ProtocolError(code, status);
}
export function object(value, names) {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value), 'INVALID_OBJECT');
  requireValue(Object.keys(value).sort().join('|') === [...names].sort().join('|'), 'INVALID_FIELDS');
}
export function uuid(value) {
  requireValue(typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value), 'INVALID_UUID');
  return value;
}
export function digest(value) {
  requireValue(typeof value === 'string' && /^[0-9a-f]{64}$/.test(value), 'INVALID_DIGEST');
  return value;
}
export function nodeID(value, prefix) {
  requireValue(typeof value === 'string' && value.length <= 256 &&
    value.startsWith(prefix) && /^[A-Za-z0-9_-]+$/.test(value), 'INVALID_NODE_ID');
  return value;
}
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort()
    .map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export async function sha256(value) {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
    .map(x => x.toString(16).padStart(2, '0')).join('');
}
export function stamp(value) {
  object(value, ['field_id', 'option_id', 'value_id', 'updated_at']);
  requireValue(value.field_id === STATUS_FIELD_ID, 'FIELD_BINDING');
  requireValue(value.option_id === null ||
    typeof value.option_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.option_id), 'INVALID_OPTION');
  requireValue(value.value_id === null || typeof value.value_id === 'string' &&
    /^[A-Za-z0-9_-]{1,256}$/.test(value.value_id), 'INVALID_VALUE_ID');
  requireValue(value.updated_at === null || typeof value.updated_at === 'string' &&
    value.updated_at.length <= 64 && Number.isFinite(Date.parse(value.updated_at)), 'INVALID_TIMESTAMP');
}
export function prepareBody(value) {
  object(value, ['operation_id', 'repository_id', 'project_id', 'item_id',
    'status_field_id', 'issue_id', 'page_id', 'sync_key', 'expected_before',
    'target', 'source_fingerprint']);
  uuid(value.operation_id); uuid(value.page_id); digest(value.source_fingerprint);
  nodeID(value.item_id, 'PVTI_');
  requireValue(value.repository_id === REPOSITORY_ID && value.project_id === PROJECT_ID &&
    value.status_field_id === STATUS_FIELD_ID, 'RESOURCE_BINDING');
  requireValue(Number.isSafeInteger(value.issue_id) && value.issue_id > 0 &&
    value.sync_key === `gh:${REPOSITORY_ID}:issue:${value.issue_id}`, 'ISSUE_BINDING');
  requireValue(typeof value.target === 'string' && Object.hasOwn(GENERAL_OPTIONS, value.target),
    'TARGET_NOT_GENERAL');
  stamp(value.expected_before);
  requireValue(Object.values(GENERAL_OPTIONS).includes(value.expected_before.option_id), 'BEFORE_NOT_GENERAL');
  return value;
}
export function boundBody(value, extra = []) {
  object(value, ['operation_id', 'binding', ...extra]); uuid(value.operation_id); digest(value.binding);
}
export function mode(env) {
  const value = env.RELAY_MODE ?? 'STOPPED';
  requireValue(['STOPPED', 'DRAINING', 'ACTIVE'].includes(value), 'INVALID_MODE', 503);
  return value;
}
export async function boundedBody(request) {
  requireValue(request.headers.get('content-type')?.split(';')[0].trim() === 'application/json', 'CONTENT_TYPE', 415);
  const chunks = []; let total = 0;
  const reader = request.body?.getReader();
  requireValue(reader, 'BODY_REQUIRED');
  try {
    for (;;) {
      const {done, value} = await reader.read(); if (done) break;
      total += value.length;
      if (total > MAX_BODY) { await reader.cancel(); throw new ProtocolError('BODY_LIMIT', 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.length; }
  return bytes;
}
export function parseJSON(bytes) {
  try { return JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes)); }
  catch { throw new ProtocolError('INVALID_JSON', 400); }
}
async function verifySigned(request, bytes, secret, domain, now = Date.now()) {
  requireValue(typeof secret === 'string' && secret.length >= 32, 'KEY_UNCONFIGURED', 503);
  const timestamp = request.headers.get('x-replica-timestamp');
  const signature = request.headers.get('x-replica-signature');
  requireValue(typeof timestamp === 'string' && /^\d{10}$/.test(timestamp) &&
    Math.abs(now / 1000 - Number(timestamp)) <= 300, 'AUTH_TIME', 401);
  requireValue(typeof signature === 'string' && /^[0-9a-f]{64}$/.test(signature), 'AUTH_SIGNATURE', 401);
  const prefix = new TextEncoder().encode(`${domain}\nPOST\n${new URL(request.url).pathname}\n${timestamp}\n`);
  const input = new Uint8Array(prefix.length + bytes.length); input.set(prefix); input.set(bytes, prefix.length);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret),
    {name:'HMAC', hash:'SHA-256'}, false, ['verify']);
  const sig = new Uint8Array(signature.match(/../g).map(x => parseInt(x, 16)));
  requireValue(await crypto.subtle.verify('HMAC', key, sig, input), 'AUTH_SIGNATURE', 401);
}
export async function verifyHelper(request,bytes,secret,now=Date.now()) {
  return verifySigned(request,bytes,secret,'replica-helper-v1',now);
}
export async function verifyWorkflow(request,bytes,env,now=Date.now()) {
  requireValue(typeof env.HELPER_REQUEST_KEY==='string'&&env.HELPER_REQUEST_KEY.length>=32&&
    env.HELPER_REQUEST_KEY!==env.WORKFLOW_CALLBACK_KEY,'KEY_SEPARATION',503);
  return verifySigned(request,bytes,env.WORKFLOW_CALLBACK_KEY,'replica-workflow-v1',now);
}
