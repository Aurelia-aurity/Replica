import {ProtocolError, requireValue, uuid, canonical, sha256, mode, object,
  parseJSON} from './protocol.js';

function propertyID(value) {
  requireValue(typeof value === 'string' && value.length > 0 && value.length <= 256, 'PROPERTY_BINDING');
  let decoded;
  try { decoded = decodeURIComponent(value); } catch { throw new ProtocolError('PROPERTY_BINDING', 400); }
  requireValue(!/[\x00-\x1f\x7f]/.test(decoded), 'PROPERTY_BINDING');
  return decoded;
}
export async function verifyNotion(request, bytes, secret) {
  requireValue(typeof secret === 'string' && secret.length >= 32, 'NOTION_KEY_UNCONFIGURED', 503);
  const header = request.headers.get('x-notion-signature');
  requireValue(typeof header === 'string' && /^sha256=[0-9a-f]{64}$/.test(header), 'NOTION_SIGNATURE', 401);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret),
    {name:'HMAC', hash:'SHA-256'}, false, ['verify']);
  const signature = new Uint8Array(header.slice(7).match(/../g).map(x => parseInt(x,16)));
  requireValue(await crypto.subtle.verify('HMAC', key, signature, bytes), 'NOTION_SIGNATURE', 401);
}
export function semanticEvent(event, env) {
  requireValue(event !== null && typeof event === 'object' && !Array.isArray(event), 'INVALID_EVENT');
  const allowed = ['id','timestamp','workspace_id','workspace_name','integration_id','subscription_id',
    'type','authors','accessible_by','attempt_number','api_version','entity','data'];
  requireValue(Object.keys(event).every(k => allowed.includes(k)), 'EVENT_FIELDS');
  uuid(event.id); uuid(event.workspace_id); uuid(event.integration_id); uuid(event.subscription_id);
  requireValue(event.workspace_id === uuid(env.NOTION_WORKSPACE_ID) &&
    event.integration_id === uuid(env.NOTION_INTEGRATION_ID) &&
    event.subscription_id === uuid(env.NOTION_SUBSCRIPTION_ID), 'SUBSCRIPTION_BINDING');
  requireValue(event.type === 'page.properties_updated', 'EVENT_TYPE');
  object(event.entity, ['id','type']); uuid(event.entity.id);
  requireValue(event.entity.type === 'page', 'ENTITY_TYPE');
  requireValue(typeof event.timestamp === 'string' && event.timestamp.length <= 64 &&
    Number.isFinite(Date.parse(event.timestamp)), 'EVENT_TIMESTAMP');
  requireValue(event.data !== null && typeof event.data === 'object' && !Array.isArray(event.data) &&
    Object.keys(event.data).every(k => ['parent','updated_properties'].includes(k)), 'EVENT_DATA');
  requireValue(Array.isArray(event.data.updated_properties) &&
    event.data.updated_properties.length > 0 && event.data.updated_properties.length <= 100,
    'EVENT_PROPERTIES');
  const properties = [...new Set(event.data.updated_properties.map(propertyID))].sort();
  requireValue(properties.includes(propertyID(env.NOTION_TASK_PROPERTY_ID)), 'PROPERTY_NOT_SELECTED');
  // Authors, workspace display name and delivery attempt are not state commands.
  return {id:event.id, timestamp:new Date(event.timestamp).toISOString(),
    workspace_id:event.workspace_id,integration_id:event.integration_id,
    subscription_id:event.subscription_id,type:event.type,entity:event.entity,
    properties};
}
export async function receiveNotion(request, bytes, env, now = Math.floor(Date.now()/1000)) {
  await verifyNotion(request, bytes, env.NOTION_VERIFICATION_TOKEN);
  const event = semanticEvent(parseJSON(bytes), env);
  const semanticDigest = await sha256(canonical(event));
  const id = `notion:${event.id}`;
  const db = env.DB;
  const prior = await db.prepare('SELECT * FROM receipts WHERE receipt_id=?').bind(id).first();
  if (prior) {
    if (prior.semantic_digest !== semanticDigest) {
      await db.prepare("UPDATE receipts SET state='fenced',reason_code='EVENT_CONTRADICTION' WHERE receipt_id=?")
        .bind(id).run();
      throw new ProtocolError('EVENT_CONTRADICTION',409);
    }
    return {receipt_id:id,state:prior.state,duplicate:true};
  }
  requireValue(mode(env) === 'ACTIVE', 'RECEIPT_DISABLED',503);
  const metadata = canonical({event_id:event.id,page_id:event.entity.id,
    property_ids:event.properties,timestamp:event.timestamp});
  // Leave capacity for already-authorized helper outcomes; no eviction or paid upgrade.
  const maxNotionReceipts = 19000;
  await db.batch([
    db.prepare(`UPDATE relay_meta SET generation=generation+1 WHERE singleton=1
      AND NOT EXISTS(SELECT 1 FROM receipts WHERE receipt_id=?) AND
      (SELECT COUNT(*) FROM receipts WHERE source='notion') < ?`).bind(id,maxNotionReceipts),
    db.prepare(`INSERT OR IGNORE INTO receipts
      (receipt_id,source,semantic_digest,page_id,sync_key,metadata,generation,state,created_at)
      SELECT ?,'notion',?,?,NULL,?,generation,'queued',? FROM relay_meta WHERE singleton=1 AND
      (SELECT COUNT(*) FROM receipts WHERE source='notion') < ?`).bind(
      id,semanticDigest,event.entity.id,metadata,now,maxNotionReceipts),
  ]);
  const receipt = await db.prepare('SELECT * FROM receipts WHERE receipt_id=?').bind(id).first();
  requireValue(receipt, 'JOURNAL_CAPACITY',503);
  if (receipt.semantic_digest !== semanticDigest) {
    await db.prepare("UPDATE receipts SET state='fenced',reason_code='EVENT_CONTRADICTION' WHERE receipt_id=?")
      .bind(id).run();
    throw new ProtocolError('EVENT_CONTRADICTION',409);
  }
  return {receipt_id:id,state:receipt.state,duplicate:false};
}
export async function bootstrap(request, bytes, env, now = Math.floor(Date.now()/1000)) {
  requireValue(mode(env) === 'STOPPED' && env.BOOTSTRAP_ENABLED === 'true', 'BOOTSTRAP_DISABLED',503);
  const expires = Number(env.BOOTSTRAP_EXPIRES_AT);
  requireValue(Number.isSafeInteger(expires) && expires > now && expires <= now+900,
    'BOOTSTRAP_EXPIRED',503);
  requireValue(typeof env.BOOTSTRAP_NONCE === 'string' && /^[0-9a-f]{64}$/.test(env.BOOTSTRAP_NONCE) &&
    new URL(request.url).searchParams.get('setup') === env.BOOTSTRAP_NONCE, 'BOOTSTRAP_NONCE',401);
  const body = parseJSON(bytes);object(body,['verification_token']);
  requireValue(typeof body.verification_token === 'string' && body.verification_token.length >= 32 &&
    new TextEncoder().encode(body.verification_token).length <= 128, 'BOOTSTRAP_TOKEN');
  let publicJWK;
  try { publicJWK = JSON.parse(env.BOOTSTRAP_PUBLIC_JWK); } catch { throw new ProtocolError('BOOTSTRAP_KEY',503); }
  requireValue(publicJWK?.kty === 'RSA' && !['d','p','q','dp','dq','qi','oth'].some(k => k in publicJWK),
    'BOOTSTRAP_KEY',503);
  const key = await crypto.subtle.importKey('jwk', publicJWK,
    {name:'RSA-OAEP',hash:'SHA-256'}, false,['encrypt']);
  requireValue(key.algorithm.modulusLength >= 2048, 'BOOTSTRAP_KEY',503);
  const encrypted = new Uint8Array(await crypto.subtle.encrypt({name:'RSA-OAEP'},key,
    new TextEncoder().encode(body.verification_token)));
  const ciphertext = btoa(String.fromCharCode(...encrypted));
  const result = await env.DB.prepare(`INSERT OR IGNORE INTO bootstrap_candidate
    (singleton,ciphertext,public_key_digest,expires_at,created_at) VALUES(1,?,?,?,?)`)
    .bind(ciphertext,await sha256(canonical(publicJWK)),expires,now).run();
  requireValue(result.meta.changes === 1, 'BOOTSTRAP_SLOT_OCCUPIED',409);
  // This receipt is never runtime trust. Operator must decrypt/verify/set the secret.
  return {candidate_stored:true,runtime_verified:false};
}
