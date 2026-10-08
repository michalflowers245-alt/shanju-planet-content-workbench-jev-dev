import type {
  DouyinExtraction,
  PlatformBrowserStatus,
  RadarSearchBatch,
  TopicRadarItem,
  TopicRadarPlatformSummary,
  TopicRadarRecommendation,
  TopicRadarRun,
  XiaohongshuArchiveBatch,
  XiaohongshuExtraction,
} from './types'

export function canResumeRadarDetails(batch?: Pick<RadarSearchBatch, 'status' | 'counts'> | null): boolean {
  return Boolean(batch && batch.counts.collected > 0
    && ['COMPLETED', 'PARTIAL', 'USER_ACTION_REQUIRED', 'AWAITING_JEV_KEY', 'INTERRUPTED', 'FAILED', 'CANCELED'].includes(batch.status))
}

export function isPendingXhsExtraction(item: Pick<XiaohongshuExtraction, 'status' | 'archive_job'>): boolean {
  if (item.archive_job) return ['QUEUED', 'RUNNING'].includes(item.archive_job.status)
  return ['queued', 'extracting', 'ready_to_archive'].includes(item.status)
}

export function isPendingXhsArchiveBatch(batch?: Pick<XiaohongshuArchiveBatch, 'counts'> | null): boolean {
  return Boolean(batch && (batch.counts.waiting > 0 || batch.counts.running > 0))
}

export function isPendingDouyinExtraction(item: Pick<DouyinExtraction, 'status' | 'asr_status'>): boolean {
  return ['QUEUED', 'PROCESSING', 'TRANSCRIBING'].includes(item.status) || item.asr_status === 'PROCESSING'
}

export function douyinTranscriptQuality(item: Pick<DouyinExtraction, 'asr_text' | 'asr_quality'>) {
  const status = item.asr_quality?.status || (item.asr_text ? 'UNKNOWN' : 'NOT_AVAILABLE')
  const labels = {
    NOT_AVAILABLE: '尚无口播转写',
    NEEDS_REVIEW: '口播转写需人工核对',
    UNKNOWN: '口播转写质量未评估',
    INCOMPLETE: '口播转写存在缺口',
    REVIEW_NOT_FLAGGED: '口播转写未触发复核阈值',
  }
  return {
    status,
    label: labels[status],
    needsReview: ['NEEDS_REVIEW', 'INCOMPLETE'].includes(status),
    reasons: item.asr_quality?.reasons || (item.asr_text ? ['缺少转写质量评估，请人工核对。'] : []),
  }
}

export function canRetryDouyinTranscript(item: Pick<DouyinExtraction, 'status' | 'asr_status' | 'asr_text' | 'asr_quality'>): boolean {
  if (!['PARTIAL', 'COMPLETE', 'USER_ACTION_REQUIRED', 'NEEDS_BROWSER', 'NEEDS_ATTENTION', 'FAILED'].includes(item.status)) return false
  const hasTranscript = Boolean(item.asr_text) || ['BROWSER_MEDIA_TRANSCRIBED', 'USER_MEDIA_TRANSCRIBED'].includes(item.asr_status)
  return !hasTranscript || douyinTranscriptQuality(item).needsReview
}

export function isCompleteDouyinPublishedCopy(item?: Pick<DouyinExtraction,
  'status' | 'video_id' | 'observed_video_id' | 'published_body' | 'published_body_source'
> | null): boolean {
  return Boolean(item && item.status === 'COMPLETE'
    && item.video_id && item.video_id === item.observed_video_id
    && item.published_body_source === '详情页结构化发布文案'
    && item.published_body?.trim())
}

export function hasRetainedDouyinCaption(item: DouyinExtraction): boolean {
  const lastAttempt = String(item.diagnostics?.last_attempt_status || '')
  return isCompleteDouyinPublishedCopy(item) && (
    item.failure_stage === 'caption_refresh_partial_preserved'
    || Boolean(lastAttempt && lastAttempt !== 'COMPLETE')
  )
}

/** Transport readiness is separate from each platform's login/verification. */
export function collectorBrowserState(status?: PlatformBrowserStatus | null): {
  ready: boolean
  label: string
  openLabel: string
} {
  if (!status) return { ready: false, label: '正在检查', openLabel: '打开双平台浏览器' }
  if (!status.available) return { ready: false, label: '未安装浏览器', openLabel: '打开双平台浏览器' }
  if (status.collector_state === 'BROWSER_PAGE_REQUIRED' || (status.running && status.page_count === 0)) {
    return { ready: false, label: '需要打开采集窗口', openLabel: '打开双平台浏览器' }
  }
  if (!status.running || status.collector_state === 'OFFLINE') {
    return { ready: false, label: '等待打开', openLabel: '打开双平台浏览器' }
  }
  if (status.collector_state === 'UNKNOWN') {
    return { ready: false, label: '连接待检查', openLabel: '打开双平台浏览器' }
  }
  return { ready: true, label: '采集连接可用', openLabel: '打开登录或验证页' }
}

export type DailyVerificationPhase = 'pending' | 'running' | 'verified' | 'action_required'

export type DailyVerificationState = {
  phase: DailyVerificationPhase
  liveItems: TopicRadarItem[]
  liveRecommendations: TopicRadarRecommendation[]
  verifiedPlatforms: string[]
  blockedPlatforms: string[]
  hiddenHistoricalCount: number
  message: string
}

const DAILY_PLATFORMS = ['xiaohongshu', 'douyin'] as const
// The managed collector reads the current rendered platform page in a
// short-lived background target.  It is live evidence just like older
// visible_browser rows, while cached/index records remain excluded.
const LIVE_SOURCE_MODES = new Set([
  'visible_browser',
  'managed_background_browser',
  'browser_verified',
  'verified_browser',
])
const BLOCKED_STATUS = /(?:login|verification|captcha|challenge|blocked|error|timeout|browser_missing|browser_not_running|needs_setup|manual)/i
const BLOCKED_MESSAGE = /(?:登录|验证|验证码|风控|失败|超时|浏览器.*(?:未打开|不可用))/

export function isRealtimeVerifiedPlatform(summary?: TopicRadarPlatformSummary): boolean {
  if (!summary || !LIVE_SOURCE_MODES.has(String(summary.source_mode || ''))) return false
  // New backend runs carry an explicit proof bit.  Never infer verification
  // from a success-looking status when the collector says it did not finish.
  if (summary.live_verified === false) return false
  const status = String(summary.status || '')
  const message = String(summary.message || '')
  return !BLOCKED_STATUS.test(status) && !BLOCKED_MESSAGE.test(message)
}

export function isRealtimeVerifiedItem(item: TopicRadarItem): boolean {
  return LIVE_SOURCE_MODES.has(String(item.source_mode || ''))
}

function recommendationEvidenceUrls(value: TopicRadarRecommendation): string[] {
  return [value.evidence_url, value.source_url, ...(value.source_urls || [])]
    .filter((url): url is string => typeof url === 'string' && /^https:\/\//i.test(url))
}

/**
 * Do not let a cached/public-index card pass itself off as a freshly verified
 * platform result.  The backend can still retain those rows for diagnostics,
 * but the daily creator view uses only current browser-visible evidence.
 */
export function dailyVerificationState(run?: TopicRadarRun | null, refreshing = false): DailyVerificationState {
  const items = run?.items || []
  const liveItems = items.filter(isRealtimeVerifiedItem)
  const liveUrls = new Set(liveItems.map(item => item.url).filter(Boolean))
  const values = (run?.summary?.recommendations || run?.summary?.recommended_topics || [])
  const liveRecommendations = values.filter((value): value is TopicRadarRecommendation => {
    if (!value || typeof value === 'string' || typeof value !== 'object') return false
    return recommendationEvidenceUrls(value).some(url => liveUrls.has(url))
  })
  const summaries = run?.summary?.platforms || []
  const verifiedPlatforms = DAILY_PLATFORMS.filter(platform =>
    isRealtimeVerifiedPlatform(summaries.find(item => item.platform === platform)),
  )
  const blockedPlatforms = DAILY_PLATFORMS.filter(platform => !verifiedPlatforms.includes(platform))
  const hiddenHistoricalCount = items.length - liveItems.length

  if (!run) {
    return {
      phase: 'pending', liveItems, liveRecommendations, verifiedPlatforms, blockedPlatforms,
      hiddenHistoricalCount: 0,
      message: '还没有开始本轮实时核验。',
    }
  }
  if (refreshing || run.status === 'RUNNING') {
    return {
      phase: 'running', liveItems: [], liveRecommendations: [], verifiedPlatforms, blockedPlatforms,
      hiddenHistoricalCount: items.length,
      message: '正在逐个平台读取和核验；完成前不会用上次保存的内容替代本轮结果。',
    }
  }
  if (blockedPlatforms.length) {
    const labels = blockedPlatforms.map(platform => platform === 'xiaohongshu' ? '小红书' : '抖音').join('、')
    return {
      phase: 'action_required', liveItems, liveRecommendations: [], verifiedPlatforms, blockedPlatforms,
      hiddenHistoricalCount,
      message: `${labels} 尚未完成本轮实时核验。已保存的历史帖子不会显示为今天的新热点。`,
    }
  }
  return {
    phase: 'verified', liveItems, liveRecommendations, verifiedPlatforms, blockedPlatforms,
    hiddenHistoricalCount,
    message: liveItems.length
      ? `本轮已完成双平台实时核验，使用 ${liveItems.length} 条当前页面内容。`
      : '本轮已完成双平台实时核验，但没有发现新的匹配帖子。',
  }
}
