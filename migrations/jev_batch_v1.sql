PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jev_settings (
  workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL DEFAULT 'https://api.typesafe.ai/v1/systemone',
  model TEXT NOT NULL DEFAULT 'jev-latest',
  secret_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  last_checked_at TEXT,
  last_error_code TEXT,
  last_error_message TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_search_batches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  origin TEXT NOT NULL DEFAULT 'topic_search',
  persona_id TEXT NOT NULL,
  persona_snapshot_json TEXT NOT NULL DEFAULT '{}',
  topic TEXT NOT NULL,
  platforms_json TEXT NOT NULL DEFAULT '["xiaohongshu","douyin"]',
  target_count INTEGER NOT NULL DEFAULT 300,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  stage TEXT NOT NULL DEFAULT 'QUEUED',
  counts_json TEXT NOT NULL DEFAULT '{}',
  platform_status_json TEXT NOT NULL DEFAULT '{}',
  taxonomy_version TEXT NOT NULL DEFAULT 'matchmaking-radar-v1',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_radar_search_batches_workspace
  ON radar_search_batches(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_search_batches_status
  ON radar_search_batches(status, updated_at);

CREATE TABLE IF NOT EXISTS radar_batch_queries (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES radar_search_batches(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  query TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  result_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  UNIQUE(batch_id, platform, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_radar_batch_queries_pending
  ON radar_batch_queries(batch_id, platform, status, ordinal);

CREATE TABLE IF NOT EXISTS radar_batch_candidates (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES radar_search_batches(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  source_key TEXT NOT NULL,
  source_mode TEXT NOT NULL DEFAULT 'managed_browser_hidden_page',
  verification_status TEXT NOT NULL DEFAULT 'LIVE_VERIFIED',
  search_query TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL DEFAULT '{}',
  classification_status TEXT NOT NULL DEFAULT 'PENDING',
  route TEXT,
  pillar TEXT,
  audience_need TEXT,
  composite_score REAL,
  confidence REAL,
  risk_flags_json TEXT NOT NULL DEFAULT '[]',
  jev_json TEXT NOT NULL DEFAULT '{}',
  discovered_at TEXT NOT NULL,
  classified_at TEXT,
  UNIQUE(batch_id, platform, source_key)
);

CREATE INDEX IF NOT EXISTS idx_radar_batch_candidates_batch
  ON radar_batch_candidates(batch_id, route, composite_score DESC, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_batch_candidates_workspace
  ON radar_batch_candidates(workspace_id, platform, discovered_at DESC);

CREATE TABLE IF NOT EXISTS radar_candidate_classifications (
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES radar_batch_candidates(id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES radar_search_batches(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  rubric_version TEXT NOT NULL,
  route TEXT NOT NULL,
  composite_score REAL NOT NULL,
  confidence REAL NOT NULL,
  result_json TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_radar_candidate_classifications_candidate
  ON radar_candidate_classifications(candidate_id, created_at DESC);

INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '13');
