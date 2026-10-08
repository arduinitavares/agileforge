-- Test-only structural release, independent of current SQLModel metadata.
CREATE TABLE schema_release_receipts (receipt_id INTEGER NOT NULL, release_name TEXT NOT NULL, PRIMARY KEY (receipt_id));
