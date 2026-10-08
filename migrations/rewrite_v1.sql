CREATE TABLE IF NOT EXISTS rewrite_jobs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_extraction_id TEXT NOT NULL,
  source_archive_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  request_json TEXT NOT NULL DEFAULT '{}',
  source_snapshot_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'QUEUED',
  stage TEXT NOT NULL DEFAULT 'QUEUED',
  revision INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,source_extraction_id) REFERENCES xhs_post_extractions(workspace_id,id),
  FOREIGN KEY(workspace_id,source_archive_id) REFERENCES xhs_archive_versions(workspace_id,id)
);

CREATE INDEX IF NOT EXISTS idx_rewrite_jobs_workspace
  ON rewrite_jobs(workspace_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rewrite_jobs_source
  ON rewrite_jobs(workspace_id,source_archive_id,created_at DESC);

CREATE TABLE IF NOT EXISTS rewrite_job_steps (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES rewrite_jobs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  prompt_version TEXT NOT NULL DEFAULT 'rewrite-v1',
  model_profile_id TEXT,
  latency_ms INTEGER,
  input_json TEXT NOT NULL DEFAULT '{}',
  output_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id,stage,revision)
);

CREATE INDEX IF NOT EXISTS idx_rewrite_steps_job
  ON rewrite_job_steps(job_id,created_at ASC);

CREATE TABLE IF NOT EXISTS radar_favorites (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  source_key TEXT NOT NULL,
  source_item_id TEXT,
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,platform,source_key)
);

CREATE INDEX IF NOT EXISTS idx_radar_favorites_workspace
  ON radar_favorites(workspace_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_topic_monitor_seen_source
  ON topic_monitor_items(workspace_id,monitor_id,platform,source_key);
