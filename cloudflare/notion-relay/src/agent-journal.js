import {ProtocolError, requireValue, prepareBody, boundBody, canonical, sha256,
  mode, digest, stamp, GENERAL_OPTIONS} from './protocol.js';

// D1's primary-only binding is intentional: no read-replica session is created.
// Every state transition plus reservation release/receipt creation is one batch.
export class AgentJournal {
  constructor(database, env, now = () => Math.floor(Date.now() / 1000)) {
    this.db = database; this.env = env; this.now = now;
  }
  query(sql, ...args) { return this.db.prepare(sql).bind(...args); }
  async operation(id) {
    return this.query('SELECT * FROM agent_operations WHERE operation_id = ?', id).first();
  }
  async bound(body, extra = []) {
    boundBody(body, extra);
    const op = await this.operation(body.operation_id);
    requireValue(op && op.binding === body.binding, 'OPERATION_BINDING', 409);
    return op;
  }
  reply(op, extra = {}) {
    return {operation_id:op.operation_id, binding:op.binding, state:op.state, ...extra};
  }
  async prepare(body) {
    prepareBody(body);
    const metadata = canonical(body), binding = await sha256(metadata);
    const prior = await this.operation(body.operation_id);
    if (prior) {
      requireValue(prior.binding === binding, 'OPERATION_CONTRADICTION', 409);
      return this.reply(prior);
    }
    requireValue(mode(this.env) === 'ACTIVE', 'PREPARE_DISABLED', 503);
    const resource = canonical([body.repository_id, body.project_id, body.item_id, body.status_field_id]);
    const now = this.now();
    await this.db.batch([
      this.query(`INSERT OR IGNORE INTO agent_operations
        (operation_id,binding,resource,metadata,state,created_at,updated_at)
        SELECT ?,?,?,?,'PREPARED',?,? WHERE NOT EXISTS
        (SELECT 1 FROM agent_reservations WHERE resource=? AND active=1)
        AND (SELECT COUNT(*) FROM agent_operations) < 1000`,
        body.operation_id, binding, resource, metadata, now, now, resource),
      this.query(`INSERT INTO agent_reservations(resource,operation_id,active)
        SELECT resource,operation_id,1 FROM agent_operations
        WHERE operation_id=? AND binding=? AND state='PREPARED'
        ON CONFLICT(resource) DO UPDATE SET operation_id=excluded.operation_id,active=1
        WHERE agent_reservations.active=0`, body.operation_id, binding),
    ]);
    const op = await this.operation(body.operation_id);
    if (!op) {
      const count = await this.query('SELECT COUNT(*) AS n FROM agent_operations').first();
      requireValue(count.n < 1000, 'JOURNAL_CAPACITY', 503);
    }
    requireValue(op && op.binding === binding, 'RESOURCE_RESERVED', 409);
    const reservation = await this.query(
      'SELECT * FROM agent_reservations WHERE resource=?', resource).first();
    requireValue(reservation?.active === 1 && reservation.operation_id === op.operation_id,
      'RESOURCE_RESERVED', 409);
    return this.reply(op);
  }
  async attempt(body) {
    const op = await this.bound(body);
    // Metadata replay NEVER reproduces a mutation permit, in any mode.
    if (op.state !== 'PREPARED') {
      requireValue(['ATTEMPTING','UNKNOWN_FENCED','REJECTED_FENCED',
        'CONFIRMED','CONFIRMED_NOOP'].includes(op.state), 'ATTEMPT_REFUSED', 409);
      return this.reply(op, {newly_authorized:false});
    }
    requireValue(mode(this.env) !== 'STOPPED', 'ATTEMPT_DISABLED', 503);
    const result = await this.query(`UPDATE agent_operations SET state='ATTEMPTING',updated_at=?
      WHERE operation_id=? AND binding=? AND state='PREPARED' AND EXISTS
      (SELECT 1 FROM agent_reservations WHERE resource=agent_operations.resource
       AND operation_id=agent_operations.operation_id AND active=1)`,
      this.now(), op.operation_id, op.binding).run();
    const current = await this.operation(op.operation_id);
    requireValue(current.state !== 'ABORTED', 'ATTEMPT_REFUSED', 409);
    return this.reply(current, {newly_authorized:result.meta.changes === 1});
  }
  async abort(body) {
    const op = await this.bound(body);
    if (op.state === 'ABORTED') return this.reply(op);
    requireValue(op.state === 'PREPARED', 'ATTEMPT_CANNOT_ABORT', 409);
    await this.db.batch([
      this.query(`UPDATE agent_operations SET state='ABORTED',updated_at=?
        WHERE operation_id=? AND binding=? AND state='PREPARED'`,
        this.now(), op.operation_id, op.binding),
      this.query(`UPDATE agent_reservations SET active=0 WHERE operation_id=? AND EXISTS
        (SELECT 1 FROM agent_operations WHERE operation_id=? AND binding=? AND state='ABORTED')`,
        op.operation_id, op.operation_id, op.binding),
    ]);
    const current = await this.operation(op.operation_id);
    requireValue(current.state === 'ABORTED', 'ATTEMPT_CANNOT_ABORT', 409);
    return this.reply(current);
  }
  async outcome(body) {
    const op = await this.bound(body, ['outcome', 'evidence_digest', 'code']);
    digest(body.evidence_digest);
    const codes = {rejected:['API_REJECTED'], unknown:['ACK_LOST','API_OUTCOME_UNKNOWN',
      'READBACK_MISMATCH','SOURCE_CHANGED','RESPONSE_IDENTITY']};
    requireValue(Object.hasOwn(codes, body.outcome) && codes[body.outcome].includes(body.code),
      'INVALID_OUTCOME');
    const target = body.outcome === 'rejected' ? 'REJECTED_FENCED' : 'UNKNOWN_FENCED';
    if (op.state === target && op.evidence_digest === body.evidence_digest && op.outcome_code === body.code)
      return this.reply(op, {sync_queued:false});
    requireValue(op.state === 'ATTEMPTING', 'OUTCOME_CONTRADICTION', 409);
    await this.query(`UPDATE agent_operations SET state=?,evidence_digest=?,outcome_code=?,updated_at=?
      WHERE operation_id=? AND binding=? AND state='ATTEMPTING'`,
      target, body.evidence_digest, body.code, this.now(), op.operation_id, op.binding).run();
    const current = await this.operation(op.operation_id);
    requireValue(current.state === target && current.evidence_digest === body.evidence_digest &&
      current.outcome_code === body.code, 'OUTCOME_CONTRADICTION', 409);
    // Retain active reservation. No receipt, generation increment or target-equality inference.
    return this.reply(current, {sync_queued:false});
  }
  async request(body) {
    const op = await this.bound(body, ['kind','evidence_digest','readback_stamp','source_fingerprint']);
    digest(body.evidence_digest); digest(body.source_fingerprint); stamp(body.readback_stamp);
    requireValue(['mutation','noop'].includes(body.kind), 'INVALID_SUCCESS_KIND');
    const metadata = JSON.parse(op.metadata);
    requireValue(body.readback_stamp.option_id === GENERAL_OPTIONS[metadata.target] &&
      body.source_fingerprint === metadata.source_fingerprint, 'SUCCESS_BINDING');
    const terminal = body.kind === 'mutation' ? 'CONFIRMED' : 'CONFIRMED_NOOP';
    if (op.state === terminal) {
      requireValue(op.evidence_digest === await sha256(canonical(body)), 'RESULT_CONTRADICTION', 409);
      return this.reply(op, {sync_queued:true});
    }
    const expected = body.kind === 'mutation' ? 'ATTEMPTING' : 'PREPARED';
    requireValue(op.state === expected, 'SUCCESS_STATE', 409);
    if (body.kind === 'noop') {
      requireValue(mode(this.env) !== 'STOPPED', 'NOOP_RECEIPT_DISABLED', 503);
      requireValue(metadata.expected_before.option_id === GENERAL_OPTIONS[metadata.target] &&
        canonical(metadata.expected_before) === canonical(body.readback_stamp), 'NOOP_BINDING');
    }
    const resultDigest = await sha256(canonical(body)), now = this.now();
    await this.db.batch([
      this.query(`UPDATE agent_operations SET state=?,evidence_digest=?,outcome_code='VERIFIED',updated_at=?
        WHERE operation_id=? AND binding=? AND state=?`, terminal,resultDigest,now,
        op.operation_id,op.binding,expected),
      this.query(`UPDATE relay_meta SET generation=generation+1 WHERE singleton=1 AND EXISTS
        (SELECT 1 FROM agent_operations WHERE operation_id=? AND state=? AND evidence_digest=?)
        AND NOT EXISTS(SELECT 1 FROM receipts WHERE receipt_id=?)`,
        op.operation_id,terminal,resultDigest,`agent:${op.operation_id}`),
      this.query(`INSERT OR IGNORE INTO receipts
        (receipt_id,source,semantic_digest,page_id,sync_key,metadata,generation,state,created_at)
        SELECT ?,'agent',?,?,?,?,generation,'queued',? FROM relay_meta WHERE singleton=1 AND EXISTS
        (SELECT 1 FROM agent_operations WHERE operation_id=? AND state=? AND evidence_digest=?)`,
        `agent:${op.operation_id}`,resultDigest,metadata.page_id,metadata.sync_key,
        canonical({operation_id:op.operation_id,kind:body.kind}),now,op.operation_id,terminal,resultDigest),
      this.query(`UPDATE agent_reservations SET active=0 WHERE operation_id=? AND EXISTS
        (SELECT 1 FROM agent_operations WHERE operation_id=? AND state=? AND evidence_digest=?)
        AND EXISTS(SELECT 1 FROM receipts WHERE receipt_id=?)`,
        op.operation_id,op.operation_id,terminal,resultDigest,`agent:${op.operation_id}`),
    ]);
    const current = await this.operation(op.operation_id);
    requireValue(current.state === terminal && current.evidence_digest === resultDigest,
      'RESULT_CONTRADICTION', 409);
    return this.reply(current, {sync_queued:true});
  }
}
