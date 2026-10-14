CREATE TABLE sync_requests (
  request_id TEXT PRIMARY KEY,
  generation INTEGER NOT NULL,
  receipt_ids TEXT NOT NULL,
  expected_sha TEXT NOT NULL,
  run_id INTEGER,
  phase TEXT NOT NULL,
  claimed_at INTEGER,
  seal_digest TEXT,
  sealed_result TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE request_slot (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  request_id TEXT REFERENCES sync_requests(request_id)
);
INSERT INTO request_slot(singleton,request_id) VALUES(1,NULL);
CREATE TABLE result_chunks (
  request_id TEXT NOT NULL REFERENCES sync_requests(request_id),
  chunk_index INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL,
  entry_count INTEGER NOT NULL,
  previous_digest TEXT NOT NULL,
  chunk_digest TEXT NOT NULL,
  first_page TEXT NOT NULL,
  last_page TEXT NOT NULL,
  PRIMARY KEY(request_id,chunk_index)
);
CREATE TABLE run_observations (
  request_id TEXT NOT NULL REFERENCES sync_requests(request_id),
  page_id TEXT NOT NULL,
  sync_key TEXT NOT NULL,
  entry TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  PRIMARY KEY(request_id,page_id),
  UNIQUE(request_id,sync_key)
);
CREATE INDEX observations_page ON run_observations(page_id);
