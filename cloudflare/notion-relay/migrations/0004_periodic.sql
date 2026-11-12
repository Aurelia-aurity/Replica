-- Bound periodic scans and cleanup to indexed lookups.
CREATE INDEX sync_requests_created ON sync_requests(created_at);
CREATE INDEX sync_requests_prune ON sync_requests(phase, receipt_ids, updated_at);
CREATE INDEX sync_requests_activated_created ON sync_requests(phase, created_at);
CREATE INDEX sync_requests_receipt_requests ON sync_requests(receipt_ids)
  WHERE receipt_ids != '[]';
CREATE INDEX receipts_request ON receipts(request_id);

-- A durable hourly attempt gate survives Worker restarts and concurrent Cron ticks.
ALTER TABLE relay_meta ADD COLUMN last_periodic_prune_at INTEGER
  CHECK(last_periodic_prune_at IS NULL OR last_periodic_prune_at >= 0);
