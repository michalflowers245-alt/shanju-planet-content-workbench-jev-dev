export type Workspace = { id: string; name: string; description: string; color: string }
export type ContentType = {
  key: string
  label: string
  platforms: string[]
  length: [number, number]
  structure: string
  rules: string[]
}

export type SkillStatus = {
  id: string
  name: string
  role: 'researcher' | 'writer' | 'editor'
  version: string
  description: string
  installed: boolean
  enabled: boolean
  status: string
}

export type ModelStatus = {
  usable: boolean
  status: string
  search_status: string
  search_mode: string
  message?: string
  active_provider?: string
  active_model?: string
  active_label?: string
  profiles: Array<Record<string, unknown>>
}

export type Task = {
  id: string
  workspace_id: string
  content_type: string
  platform: string
  topic: string
  goal: string
  offer: string
  constraints: Record<string, unknown>
  web_research: boolean
  status: string
  stage: string
  latest_score?: number
  revision_round: number
  last_error?: string
  created_at: string
  updated_at?: string
  source_case_id?: string | null
}

export type Source = {
  id: string
  source_key: string
  kind: string
  title: string
  url: string
  excerpt: string
  credibility: number
  route?: 'A' | 'B' | 'C' | null
  classification_status?: string
  jev?: { route?: 'A' | 'B' | 'C' | null; status?: string; reason?: string; confidence?: number; composite_score?: number }
  published_at?: string
  metadata?: {
    search_mode?: string
    searched_at?: string
    search_focus?: string
    platform?: 'xiaohongshu' | 'douyin' | 'web'
    author?: string
    metrics?: Record<string, number | string | null>
    account_relation?: string
    content_direction?: string
    xhs_extraction?: XiaohongshuExtraction
  }
}

export type XiaohongshuExtraction = {
  id: string
  workspace_id: string
  url: string
  post_key: string
  search_keyword: string
  title: string
  author: string
  published_at?: string | null
  body: string
  image_text: string
  video_subtitle: string
  video_speech: string
  merged_copy: string
  tags: string[]
  metrics: Record<string, number | string | null>
  screenshot_url: string
  status: string
  completeness: number
  field_sources: Record<string, string>
  ocr_status: string
  ocr_message: string
  asr_status: string
  asr_message: string
  failure_stage: string
  error_message: string
  diagnostics: Record<string, unknown>
  retry_count: number
  last_attempt_at: string
  extracted_at: string
  updated_at: string
  body_raw?: string | null
  body_normalized?: string | null
  body_source?: string
  body_extraction_status?: string
  body_evidence?: Record<string, unknown>
  archive_status?: string
  verification_status?: string
  content_sha256?: string | null
  latest_archive_id?: string | null
  expected_asset_count?: number | null
  saved_asset_count?: number
  archive?: XiaohongshuArchive | null
  archive_job?: XiaohongshuArchiveJob | null
}

/** A Douyin detail is evidence-separated by design.  Do not join these
 * fields: the platform caption, subtitle, local ASR and OCR have different
 * reliability and scope. */
export type DouyinExtraction = {
  id: string
  workspace_id: string
  url: string
  video_id: string
  observed_video_id: string
  search_keyword: string
  title: string
  author: string
  published_at?: string | null
  metrics: Record<string, number | string | null>
  search_result_text: string
  search_result_text_source: string
  published_body: string
  published_body_source: string
  platform_subtitle: string
  subtitle_source: string
  asr_status: string
  asr_text: string
  asr_quality?: {
    status: 'NOT_AVAILABLE' | 'NEEDS_REVIEW' | 'UNKNOWN' | 'INCOMPLETE' | 'REVIEW_NOT_FLAGGED'
    reasons: string[]
    threshold: number
    mean_confidence: number | null
    assessed_segment_count: number
    low_confidence_segment_count: number
  }
  asr_segments: Array<{
    text: string
    start_seconds: number | null
    end_seconds: number | null
    confidence: number | null
  }>
  asr_language: string
  asr_engine: string
  asr_model: string
  asr_source: string
  asr_message: string
  asr_media_sha256: string
  asr_confidence: {
    mean?: number
    minimum?: number
    maximum?: number
    assessed_segment_count?: number
    low_confidence_threshold?: number
  }
  asr_low_confidence: Array<{
    text: string
    start_seconds: number | null
    end_seconds: number | null
    confidence: number | null
  }>
  ocr_status: string
  ocr_text: string
  video_status: string
  status: string
  failure_stage: string
  error_message: string
  diagnostics: Record<string, unknown>
  retry_count: number
  last_attempt_at: string
  extracted_at: string
  updated_at: string
}

export type XiaohongshuArchiveAsset = {
  id: string
  role: 'carousel' | 'cover' | string
  ordinal: number
  mime_detected?: string | null
  extension?: string | null
  width?: number | null
  height?: number | null
  byte_size?: number | null
  sha256?: string | null
  status: string
  error_code?: string | null
  download_url?: string
}

export type XiaohongshuRichToken = {
  type: 'text' | 'break' | 'inline_image' | 'unresolved' | string
  text?: string
  alt?: string
  label?: string
  kind?: string
  asset_role?: string
  asset_ordinal?: number
  width?: number | null
  height?: number | null
}

export type XiaohongshuArchive = {
  id: string
  extraction_id: string
  post_id: string
  observed_post_id: string
  body_raw?: string | null
  body_normalized?: string | null
  body_source: string
  body_extraction_status?: string
  body_extraction_message?: string
  archive_status: string
  verification_status: string
  content_sha256?: string | null
  manifest_sha256?: string | null
  captured_at: string
  body_sha256?: string | null
  body_evidence?: Record<string, unknown>
  parser_version?: string
  body_rich?: XiaohongshuRichToken[]
  assets: XiaohongshuArchiveAsset[]
}

export type XiaohongshuArchiveBatch = {
  id: string
  status: string
  selected_count: number
  counts: Record<'selected' | 'waiting' | 'running' | 'complete' | 'partial' | 'paused' | 'failed' | 'invalid', number>
  items: Array<{ id: string; ordinal: number; extraction_id: string; url: string; title: string; status: string; progress?: { message?: string } }>
}

export type XiaohongshuArchiveJob = {
  id: string
  extraction_id: string
  status: string
  stage: string
  error_code?: string | null
  progress: {
    message?: string
    archive_id?: string
    expected_asset_count?: number | null
    saved_asset_count?: number
    missing_ordinals?: number[]
  }
  updated_at: string
}

export type DraftPackage = {
  title: string
  alternative_titles: string[]
  body: string
  strategy?: DraftStrategy
  cta: string
  tags?: string[]
  claims?: Array<{ claim: string; evidence_ids: string[]; status: string }>
  platform_variants?: Record<string, string>
  conversion_structure?: Record<string, string>
  deliverables?: ProductDeliverables
}

export type DraftStrategy = {
  surface_question?: string
  deeper_question?: string
  persona_fit?: string
  business_fit?: string
  real_scene?: string
  controlling_idea?: string
  editorial_promise?: string
  old_logic?: string
  what_changed?: string
  reader_cost?: string
  reader_decision?: string
  selected_opening?: string
  voice_direction?: string
  argument_beats?: Array<{ purpose?: string; reader_gain?: string; bridge?: string; must_not_do?: string; evidence_ids?: string[] }>
  causal_chain?: Array<{ cause?: string; change?: string; behavior?: string; cost?: string; evidence_ids?: string[] }>
  fact_boundaries?: string[]
  anti_repetition?: string[]
  must_preserve?: string[]
}

export type MomentsPost = {
  day: number
  time: string
  role: string
  purpose: string
  content: string
  follow_up: string
}

export type ProductDeliverables = {
  moments_posts: MomentsPost[]
  xiaohongshu_publish: { title: string; body: string; tags: string[] }
  douyin_script: { hook: string; script: string; emotion_beats: string[]; ending: string }
  life_case_variants?: LifeCaseVariant[]
}

export type LifeCaseVariant = {
  angle: 'daily' | 'reflection' | 'soft_business' | string
  label: string
  body: string
  rationale?: string
  recommended: boolean
}

export type LifeCaseMedia = {
  id: string
  display_name: string
  mime_type: string
  byte_size: number
  width?: number | null
  height?: number | null
  sha256?: string
  ordinal: number
  media_url: string
}

export type LifeCase = {
  id: string
  workspace_id: string
  title: string
  note_text: string
  occurred_at?: string | null
  sync_ip: boolean
  archived: boolean
  created_at: string
  updated_at?: string
  media: LifeCaseMedia[]
  latest_task_id?: string | null
  latest_task_status?: string | null
}

export type Draft = {
  id: string
  task_id: string
  version: number
  origin: string
  package: DraftPackage
  body_text: string
  is_final: boolean
  created_at: string
}

export type Review = {
  id: string
  draft_version_id: string
  round: number
  scores: Record<string, number>
  total_score: number
  blocking_issues: string[]
  instructions: string[]
  change_summary: string
  conversion_structure: Record<string, string>
  risks: string[]
  decision: string
}

export type TaskArtifact = {
  id: string
  task_id: string
  artifact_type: string
  round: number
  payload: Record<string, unknown>
  body_text: string
  created_at: string
}

export type TaskEvent = {
  id: number
  stage: string
  event_type: string
  message: string
  created_at: string
}

export type TaskDetail = {
  task: Task
  research: null | { packet: Record<string, unknown> }
  sources: Source[]
  drafts: Draft[]
  reviews: Review[]
  events: TaskEvent[]
  artifacts: TaskArtifact[]
  assets: Array<Record<string, unknown>>
  expression_checks: Array<Record<string, unknown>>
  source_case?: LifeCase | null
}

export type Bootstrap = {
  workspaces: Workspace[]
  content_types: ContentType[]
  model_status: ModelStatus
  skills: SkillStatus[]
  publishing_enabled: boolean
}

export type LocalProxySettings = {
  base_url: string
  model: { status: string; model: string; message: string; latency_ms?: number }
  search: { status: string; mode: string; message: string; source_count?: number }
  checked_at: string | null
}

export type DeepSeekSettings = {
  model: { status: string; model: string; message: string; latency_ms?: number }
  base_url: string
  has_api_key: boolean
  configured: boolean
  active: boolean
  checked_at: string | null
  search?: { status: string; mode: string; message: string; source_count?: number }
}

export type WorkspaceProfile = {
  workspace_id: string
  profile: Record<string, unknown>
  completed_fields: string[]
  updated_at: string
}

export type JevSettings = {
  workspace_id: string
  endpoint: string
  model: string
  configured: boolean
  enabled: boolean
  verification_status: string
  last_checked_at?: string | null
  last_error_code?: string | null
  last_error_message?: string | null
  latency_ms?: number
}

export type RadarPersona = {
  id: string
  label: string
  public_role: string
  audience: string
  business: string
  search_terms: string[]
  pillars: Array<{ id: string; label: string; description: string }>
  guardrails: string[]
}

export type RadarBatchCandidate = {
  id: string
  batch_id: string
  workspace_id: string
  platform: 'xiaohongshu' | 'douyin'
  source_key: string
  source_mode: string
  verification_status: string
  search_query: string
  title: string
  url: string
  excerpt: string
  author: string
  published_at?: string | null
  metrics: Record<string, number | string | null>
  raw: Record<string, unknown>
  classification_status: 'PENDING' | 'CLASSIFIED' | 'ERROR' | string
  route?: 'A' | 'B' | 'C' | null
  pillar?: string | null
  audience_need?: string | null
  composite_score?: number | null
  confidence?: number | null
  risk_flags: string[]
  jev: {
    reason?: string
    scores?: Record<string, number>
    risks?: Record<string, number>
    error_code?: string
    message?: string
    model?: string
    rubric_version?: string
  }
  discovered_at: string
  classified_at?: string | null
}

export type RadarSearchBatch = {
  id: string
  workspace_id: string
  origin: 'topic_search' | 'ip_daily' | string
  persona_id: string
  persona: RadarPersona
  topic: string
  platforms: Array<'xiaohongshu' | 'douyin'>
  target_count: number
  skip_classification: boolean
  taxonomy_version: string
  status: string
  stage: string
  counts: {
    target: number
    collected: number
    classified: number
    classification_errors: number
    A: number
    B: number
    C: number
  }
  platform_status: Record<string, {
    platform: string
    status: string
    verification_status: string
    target: number
    collected: number
    message?: string
    error_code?: string
  }>
  query_counts: Record<string, Record<string, number>>
  recommendations: Array<{
    title: string
    reason?: string
    pillar?: string
    platform: string
    url: string
    score?: number
  }>
  error_message?: string | null
  cancel_requested: boolean
  started_at?: string | null
  completed_at?: string | null
  created_at: string
  updated_at: string
}

export type RadarBatchCandidatePage = {
  items: RadarBatchCandidate[]
  total: number
  limit: number
  offset: number
}

/** `web` is the public-trend source used by the profile-aware daily radar. */
export type RadarPlatform = 'xiaohongshu' | 'douyin' | 'web'

export type TopicMonitor = {
  id: string
  workspace_id: string
  query: string
  platforms: RadarPlatform[]
  interval_hours: number
  enabled: boolean
  last_run_at?: string | null
  next_run_at?: string | null
  last_status: string
  last_error?: string | null
  latest_item_count?: number
  /** Older workspaces do not have this field, so keep it optional in the UI. */
  monitor_kind?: 'manual' | 'ip_daily' | string
  created_at: string
  updated_at: string
}

export type TopicRadarPlatformSummary = {
  platform: RadarPlatform
  status: 'success' | 'error' | 'needs_setup' | 'manual_search' | string
  count: number
  source_mode: string
  message?: string
  search_url?: string
  /** Explicit backend proof for a current browser read; false always blocks daily use. */
  live_verified?: boolean
  verification_status?: 'LIVE_VERIFIED' | 'USER_ACTION_REQUIRED' | 'UNVERIFIED' | string
  attempt_count?: number
  candidate_count?: number
  previously_seen_count?: number
  freshness_mode?: string
}

export type TopicRadarItem = {
  id: string
  run_id: string
  monitor_id: string
  workspace_id: string
  platform: RadarPlatform
  source_mode: string
  source_key: string
  title: string
  url: string
  excerpt: string
  author?: string
  published_at?: string | null
  metrics: Record<string, number | string | null>
  raw: Record<string, unknown>
  is_new: boolean
  discovered_at: string
}

export type TopicRadarRun = {
  id: string
  monitor_id: string
  workspace_id: string
  status: string
  summary: {
    query?: string
    total?: number
    new_count?: number
    visible_signal_count?: number
    cached_signal_count?: number
    public_signal_count?: number
    freshness_mode?: 'new_only' | string
    previously_seen_count?: number
    platforms?: TopicRadarPlatformSummary[]
    jev_classification?: {
      status?: string
      classified?: number
      A?: number
      B?: number
      C?: number
      errors?: number
      message?: string | null
    }
    /** A short list of content angles picked from the profile-aware daily scan. */
    recommendations?: TopicRadarRecommendation[]
    recommended_topics?: TopicRadarRecommendation[]
  }
  error_message?: string | null
  started_at: string
  completed_at?: string | null
  items: TopicRadarItem[]
}

export type TopicRadarRecommendation = {
  route?: 'A' | 'B' | 'C'
  title?: string
  topic?: string
  angle?: string
  reason?: string
  why?: string
  /** Human-readable account lane, such as a local matchmaking creator or a female dating account. */
  persona?: string
  pillar?: string
  search_query?: string
  /** Only official, platform-native search actions belong on creator-facing radar cards. */
  platform_search_links?: PlatformSearchLink[]
  public_signal_count?: number
  source_url?: string
  source_urls?: string[]
  source_title?: string
  evidence_url?: string
  evidence_platform?: 'xiaohongshu' | 'douyin' | string
  visible_metrics?: Record<string, number | string | null>
  score?: number
}

export type PlatformSearchLink = {
  platform: 'douyin' | 'xiaohongshu'
  label: string
  url: string
  source_mode?: 'official_search' | string
}

/** Response of the one-off "search a topic, then create" flow.
 * The optional aliases let the UI remain compatible while the local service
 * evolves without ever treating an untyped network response as a hard error.
 */
export type TopicSearchResponse = {
  topic?: string
  sources?: Source[]
  items?: Source[]
  angles?: TopicRadarRecommendation[]
  recommendations?: TopicRadarRecommendation[]
  platform_search_links?: PlatformSearchLink[]
  search_mode?: string
  message?: string
  warning?: string | null
  visible_source_count?: number
  live_verified?: boolean
  platforms?: TopicRadarPlatformSummary[]
  jev_classification?: TopicRadarRun['summary']['jev_classification']
}

export type PlatformBrowserStatus = {
  available: boolean
  running: boolean
  collector_state?: 'BROWSER_PAGE_REQUIRED' | 'READY' | 'UNKNOWN' | 'OFFLINE'
  page_count?: number | null
  mode: string
  message: string
  profile_persistent?: boolean
  session_storage?: string
}

export type RewriteStage = 'analysis' | 'strategy' | 'copy_generation' | 'image_plan_generation' | 'review'

export type RewriteImageCard = {
  id: string
  ordinal: number
  role: string
  caption: string
  prompt: string
  related_copy_segment: string
  visual_notes: string
}

export type RewritePackage = {
  analysis?: Record<string, unknown>
  strategy?: Record<string, unknown>
  copy_generation?: {
    title: string
    alternative_titles: string[]
    body: string
    tags: string[]
    comment_cta: string
    cover_copy: string
    copy_segments: Array<{ id: string; label: string; text: string }>
  }
  image_plan_generation?: {
    style_bible: {
      visual_tone: string
      palette: string[]
      lighting: string
      continuity_rules: string[]
      avoid: string[]
    }
    image_cards: RewriteImageCard[]
  }
  review?: {
    decision: 'PASS' | 'REVISE' | string
    quality_checks: Record<string, unknown>
    issues: string[]
    corrections: string[]
    final_notes: string
  }
}

export type RewriteJob = {
  id: string
  workspace_id: string
  source_extraction_id: string
  source_archive_id: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | string
  stage: RewriteStage | 'QUEUED' | 'COMPLETED' | string
  revision: number
  request: {
    target_platform: 'xiaohongshu' | 'douyin' | 'moments'
    rewrite_strength: 'light' | 'balanced' | 'deep'
    persona: string
    image_count: number
    additional_brief?: string
  }
  source_snapshot: {
    extraction_id: string
    archive_id: string
    post_id: string
    title: string
    author: string
    url: string
    body_raw: string
    body_sha256: string
    body_source: string
    verification_status: string
    archive_status: string
    image_quality: 'COMPLETE' | 'PARTIAL' | string
    expected_image_count?: number | null
    saved_image_count: number
    images: Array<{
      asset_id: string
      role: string
      ordinal: number
      mime?: string
      extension?: string
      width?: number | null
      height?: number | null
      byte_size?: number | null
      sha256?: string
      download_url: string
    }>
  }
  result: RewritePackage
  steps: Array<{
    id: string
    stage: RewriteStage
    revision: number
    status: string
    error_code?: string | null
    error_message?: string | null
    latency_ms?: number | null
  }>
  error_code?: string | null
  error_message?: string | null
  created_at: string
  updated_at: string
}

export type RadarFavorite = {
  id: string
  workspace_id: string
  platform: RadarPlatform
  source_key: string
  source_item_id?: string | null
  url: string
  title: string
  author?: string
  excerpt?: string
  metrics: Record<string, number | string | null>
  created_at: string
  updated_at: string
}
