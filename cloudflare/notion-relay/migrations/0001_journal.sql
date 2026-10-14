-- New relay database only. No existing Project/Notion data is migrated.
PRAGMA foreign_keys = ON;
CREATE TABLE relay_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0)
);
INSERT INTO relay_meta(singleton, generation) VALUES(1, 0);
CREATE TABLE agent_operations (
  operation_id TEXT PRIMARY KEY,
  binding TEXT NOT NULL,
  resource TEXT NOT NULL,
  metadata TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('PREPARED','ATTEMPTING','ABORTED','CONFIRMED','CONFIRMED_NOOP',
     'UNKNOWN_FENCED','REJECTED_FENCED')),
  evidence_digest TEXT,
  outcome_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE agent_reservations (
  resource TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES agent_operations(operation_id),
  active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE receipts (
  receipt_id TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK(source IN ('agent','notion')),
  semantic_digest TEXT NOT NULL,
  page_id TEXT,
  sync_key TEXT,
  metadata TEXT NOT NULL,
  generation INTEGER UNIQUE NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  reason_code TEXT,
  request_id TEXT,
  dispatch_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX receipts_work ON receipts(state, generation);
