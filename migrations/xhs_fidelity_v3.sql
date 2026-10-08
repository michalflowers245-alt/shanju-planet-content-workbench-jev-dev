CREATE TABLE IF NOT EXISTS xhs_archive_batches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'SELECTED',
  selected_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xhs_archive_batch_items (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES xhs_archive_batches(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  extraction_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal>=1),
  status TEXT NOT NULL DEFAULT 'SELECTED',
  job_id TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(batch_id,extraction_id),
  UNIQUE(batch_id,ordinal),
  FOREIGN KEY(workspace_id,extraction_id) REFERENCES xhs_post_extractions(workspace_id,id)
);

CREATE INDEX IF NOT EXISTS ix_xhs_archive_batch_items_batch
  ON xhs_archive_batch_items(batch_id,ordinal);
