CREATE TABLE bootstrap_candidate (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  ciphertext TEXT NOT NULL,
  public_key_digest TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
