import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'

const server = await createServer({
  root: process.cwd(),
  logLevel: 'error',
  server: { middlewareMode: true },
  optimizeDeps: { noDiscovery: true },
})

try {
  const { canResumeRadarDetails, canRetryDouyinTranscript, collectorBrowserState, dailyVerificationState, douyinTranscriptQuality, hasRetainedDouyinCaption, isCompleteDouyinPublishedCopy, isPendingDouyinExtraction, isPendingXhsArchiveBatch, isPendingXhsExtraction } = await server.ssrLoadModule('/src/radarVerification.ts')
  const { copyPlainText, downloadPlainText, preparePlainTextExport } = await server.ssrLoadModule('/src/plainTextExport.ts')

  const browserStatus = { available: true, running: true, mode: 'managed', message: '' }
  const noPage = collectorBrowserState({ ...browserStatus, collector_state: 'BROWSER_PAGE_REQUIRED', page_count: 0 })
  assert.equal(noPage.ready, false, 'Chrome 进程存在但没有页面时不能显示可采集')
  assert.equal(noPage.label, '需要打开采集窗口')
  assert.equal(noPage.openLabel, '打开双平台浏览器')
  assert.equal(collectorBrowserState({ ...browserStatus, page_count: 0 }).ready, false, '兼容无 collector_state 的零页面状态')
  assert.equal(collectorBrowserState({ ...browserStatus, collector_state: 'UNKNOWN', page_count: null }).ready, false)
  assert.equal(collectorBrowserState({ ...browserStatus, collector_state: 'OFFLINE' }).ready, false)
  assert.equal(collectorBrowserState({ ...browserStatus, running: false, collector_state: 'READY' }).ready, false)
  const connected = collectorBrowserState({ ...browserStatus, collector_state: 'READY', page_count: 2 })
  assert.equal(connected.ready, true)
  assert.equal(connected.label, '采集连接可用', '浏览器连接成功不等于小红书或抖音已登录或已验证')
  assert.equal(collectorBrowserState(null).ready, false)
  for (const status of ['COMPLETED', 'PARTIAL', 'USER_ACTION_REQUIRED', 'AWAITING_JEV_KEY', 'INTERRUPTED', 'FAILED', 'CANCELED']) {
    assert.equal(canResumeRadarDetails({ status, counts: { collected: 212 } }), true, `${status} 搜索批次仍可单独补齐已保存候选的正文`)
  }
  for (const status of ['QUEUED', 'COLLECTING', 'CLASSIFYING']) {
    assert.equal(canResumeRadarDetails({ status, counts: { collected: 212 } }), false, '搜索进行中不能并发恢复正文队列')
  }
  assert.equal(canResumeRadarDetails({ status: 'COMPLETED', counts: { collected: 0 } }), false)
  assert.equal(canResumeRadarDetails(null), false)
  for (const status of ['PAUSED', 'FAILED', 'PARTIAL', 'COMPLETE']) {
    assert.equal(isPendingXhsExtraction({ status: 'ready_to_archive', archive_job: { status } }), false, `旧 ready_to_archive 不得盖过 ${status}，否则恢复按钮消失并永久等待`)
  }
  assert.equal(isPendingXhsExtraction({ status: 'needs_verification', archive_job: { status: 'QUEUED' } }), true)
  assert.equal(isPendingXhsExtraction({ status: 'ready_to_archive', archive_job: null }), true)
  assert.equal(isPendingXhsArchiveBatch({ counts: { waiting: 0, running: 0, paused: 3, failed: 1 } }), false, '无活动任务的暂停批次停止轮询')
  assert.equal(isPendingXhsArchiveBatch({ counts: { waiting: 1, running: 0, paused: 3 } }), true, '显式恢复一条后重新轮询')
  assert.equal(isPendingXhsArchiveBatch({ counts: { waiting: 0, running: 1, paused: 3 } }), true)
  for (const status of ['QUEUED', 'PROCESSING', 'TRANSCRIBING']) {
    assert.equal(isPendingDouyinExtraction({ status, asr_status: 'NOT_ATTEMPTED' }), true, `${status} 仍须轮询，不能永久停在处理中`)
  }
  assert.equal(isPendingDouyinExtraction({ status: 'PARTIAL', asr_status: 'PROCESSING' }), true)
  for (const status of ['COMPLETE', 'PARTIAL', 'USER_ACTION_REQUIRED', 'FAILED']) {
    assert.equal(isPendingDouyinExtraction({ status, asr_status: 'NOT_ATTEMPTED' }), false, `${status} 不应反复请求平台`)
  }
  const verifiedCopy = {
    status: 'COMPLETE', video_id: '7450123456789012345', observed_video_id: '7450123456789012345',
    published_body: ' 完整发布文案\n#测试 ', published_body_source: '详情页结构化发布文案',
  }
  assert.equal(isCompleteDouyinPublishedCopy(verifiedCopy), true)
  const lowConfidenceCopy = {
    ...verifiedCopy, asr_text: '机器转写原文照样保留', asr_status: 'BROWSER_MEDIA_TRANSCRIBED',
    asr_quality: { status: 'NEEDS_REVIEW', reasons: ['模型置信度较低，需要人工核对。'] },
  }
  assert.equal(douyinTranscriptQuality(lowConfidenceCopy).needsReview, true)
  assert.equal(canRetryDouyinTranscript(lowConfidenceCopy), true, '已有非空低置信度转写应纳入显式重做入口')
  assert.equal(isCompleteDouyinPublishedCopy(lowConfidenceCopy), true, '口播质量须独立于已核验发布正文')
  assert.equal(canRetryDouyinTranscript({ ...lowConfidenceCopy, asr_quality: { status: 'INCOMPLETE' } }), true)
  assert.equal(canRetryDouyinTranscript({ ...lowConfidenceCopy, status: 'QUEUED' }), false, '已经排队的口播不重复加入')
  for (const status of ['UNKNOWN', 'REVIEW_NOT_FLAGGED']) {
    assert.equal(canRetryDouyinTranscript({ ...lowConfidenceCopy, asr_quality: { status } }), false, `${status} 不扩大到批量重做范围`)
  }
  assert.equal(douyinTranscriptQuality({ asr_text: '旧记录未评估' }).status, 'UNKNOWN')
  assert.equal(canRetryDouyinTranscript({ ...verifiedCopy, asr_text: '', asr_status: 'NOT_ATTEMPTED' }), true)
  assert.equal(isCompleteDouyinPublishedCopy({ ...lowConfidenceCopy, published_body: '' }), false, '非空ASR不得填补平台发布文案')
  assert.equal(hasRetainedDouyinCaption({ ...verifiedCopy, failure_stage: '', diagnostics: {} }), false)
  assert.equal(hasRetainedDouyinCaption({ ...verifiedCopy, failure_stage: 'caption_refresh_partial_preserved', diagnostics: {} }), true, '旧完整正文保留成功不代表本次重读成功')
  assert.equal(hasRetainedDouyinCaption({ ...verifiedCopy, failure_stage: '', diagnostics: { last_attempt_status: 'PARTIAL' } }), true)
  for (const partial of [
    { ...verifiedCopy, status: 'PARTIAL' },
    { ...verifiedCopy, observed_video_id: '' },
    { ...verifiedCopy, observed_video_id: '7450123456789012346' },
    { ...verifiedCopy, video_id: '', observed_video_id: '' },
    { ...verifiedCopy, published_body: ' \n' },
    { ...verifiedCopy, published_body_source: '详情页元数据文案片段' },
    { ...verifiedCopy, published_body_source: '详情页分享文案片段' },
    null,
  ]) {
    assert.equal(isCompleteDouyinPublishedCopy(partial), false, '完整计数、卡片和导出不得把旧状态、ID缺失/不匹配或分享片段当完整发布文案')
  }

  const exactCaption = ' 原文首行\r\n第二行，保留 #话题 和 emoji 🥳\n'
  const preparedExport = preparePlainTextExport('抖音:/caption?*', exactCaption)
  assert.equal(preparedExport.filename, '抖音__caption__.txt')
  assert.equal(preparedExport.text, exactCaption, 'TXT 导出必须保留原文换行、空格和 emoji')

  let clipboardText = ''
  await copyPlainText(exactCaption, {
    clipboard: { writeText: async text => { clipboardText = text } },
    document: {},
  })
  assert.equal(clipboardText, exactCaption, '复制必须原样传递正文，不能 trim 或重排换行')

  let fallbackValue = ''
  let fallbackAttached = false
  let fallbackRemoved = false
  const fallbackInput = {
    value: '', style: {}, setAttribute: () => {}, select: () => {},
    remove: () => { fallbackRemoved = true },
  }
  const fallbackDocument = {
    createElement: tag => { assert.equal(tag, 'textarea'); return fallbackInput },
    body: { appendChild: input => { fallbackAttached = input === fallbackInput } },
    execCommand: command => { assert.equal(command, 'copy'); fallbackValue = fallbackInput.value; return true },
  }
  await copyPlainText(exactCaption, {
    clipboard: { writeText: async () => { throw new Error('clipboard permission denied') } },
    document: fallbackDocument,
  })
  assert.equal(fallbackAttached, true)
  assert.equal(fallbackRemoved, true)
  assert.equal(fallbackValue, exactCaption, '剪贴板 API 不可用时的回退复制仍须保留原文')

  await assert.rejects(
    copyPlainText(exactCaption, {
      clipboard: { writeText: async () => { throw new Error('clipboard permission denied') } },
      document: { ...fallbackDocument, execCommand: () => false },
    }),
    /复制失败/,
  )

  let exportedBlob
  let exportedUrl = ''
  let revokedUrl = ''
  let exportDelay = -1
  let clicked = false
  const anchor = { href: '', download: '', click: () => { clicked = true } }
  class CapturedBlob {
    constructor(parts, options) { this.parts = parts; this.type = options.type }
  }
  downloadPlainText('抖音:/caption?*', exactCaption, {
    urlApi: {
      createObjectURL: blob => { exportedBlob = blob; return (exportedUrl = 'blob:test-caption') },
      revokeObjectURL: url => { revokedUrl = url },
    },
    BlobCtor: CapturedBlob,
    document: { createElement: tag => { assert.equal(tag, 'a'); return anchor } },
    window: { setTimeout: (callback, delay) => { exportDelay = delay; callback(); return 1 } },
  })
  assert.equal(clicked, true, '导出必须实际触发浏览器下载链接')
  assert.equal(anchor.href, exportedUrl)
  assert.equal(anchor.download, '抖音__caption__.txt')
  assert.deepEqual(exportedBlob.parts, [exactCaption], '导出的 Blob 必须使用完整原文')
  assert.equal(exportedBlob.type, 'text/plain;charset=utf-8')
  assert.equal(exportDelay, 0)
  assert.equal(revokedUrl, exportedUrl, '下载触发后应回收临时 Blob URL')

  const cachedRun = {
    id: 'run-cached', status: 'DONE', items: [
      { id: 'xhs-old', platform: 'xiaohongshu', source_mode: 'cached_visible_browser', url: 'https://www.xiaohongshu.com/explore/old', source_key: 'xhs-old' },
      { id: 'dy-old', platform: 'douyin', source_mode: 'cached_visible_browser', url: 'https://www.douyin.com/video/old', source_key: 'dy-old' },
    ],
    summary: {
      platforms: [
        { platform: 'xiaohongshu', status: 'success', count: 7, source_mode: 'cached_visible_browser', message: '实时页面需要登录或验证；已显示上次保存的内容。' },
        { platform: 'douyin', status: 'success', count: 8, source_mode: 'cached_visible_browser', message: '实时页面需要登录或验证；已显示上次保存的内容。' },
      ],
      recommendations: [{ title: '历史方向', evidence_url: 'https://www.xiaohongshu.com/explore/old', source_urls: ['https://www.xiaohongshu.com/explore/old'] }],
    },
  }
  const cached = dailyVerificationState(cachedRun)
  assert.equal(cached.phase, 'action_required')
  assert.equal(cached.liveItems.length, 0)
  assert.equal(cached.liveRecommendations.length, 0)
  assert.equal(cached.hiddenHistoricalCount, 2)

  const liveRun = {
    id: 'run-live', status: 'DONE', items: [
      { id: 'xhs-current', platform: 'xiaohongshu', source_mode: 'visible_browser', url: 'https://www.xiaohongshu.com/explore/current', source_key: 'xhs-current' },
      { id: 'dy-current', platform: 'douyin', source_mode: 'visible_browser', url: 'https://www.douyin.com/video/current', source_key: 'dy-current' },
    ],
    summary: {
      platforms: [
        { platform: 'xiaohongshu', status: 'success', count: 1, source_mode: 'visible_browser' },
        { platform: 'douyin', status: 'success', count: 1, source_mode: 'visible_browser' },
      ],
      recommendations: [
        { title: '小红书实时方向', evidence_url: 'https://www.xiaohongshu.com/explore/current' },
        { title: '抖音实时方向', evidence_url: 'https://www.douyin.com/video/current' },
      ],
    },
  }
  const live = dailyVerificationState(liveRun)
  assert.equal(live.phase, 'verified')
  assert.equal(live.liveItems.length, 2)
  assert.equal(live.liveRecommendations.length, 2)
  assert.equal(live.hiddenHistoricalCount, 0)

  const incompleteBackgroundRun = {
    ...liveRun,
    summary: {
      ...liveRun.summary,
      platforms: [
        { platform: 'xiaohongshu', status: 'success', count: 1, source_mode: 'managed_background_browser', live_verified: false },
        { platform: 'douyin', status: 'success', count: 1, source_mode: 'managed_background_browser', live_verified: true },
      ],
    },
  }
  const incomplete = dailyVerificationState(incompleteBackgroundRun)
  assert.equal(incomplete.phase, 'action_required')
  assert.deepEqual(incomplete.verifiedPlatforms, ['douyin'])

  const managedBackgroundRun = {
    ...liveRun,
    items: liveRun.items.map(item => ({ ...item, source_mode: 'managed_background_browser' })),
    summary: {
      ...liveRun.summary,
      platforms: liveRun.summary.platforms.map(item => ({ ...item, source_mode: 'managed_background_browser', live_verified: true })),
    },
  }
  assert.equal(dailyVerificationState(managedBackgroundRun).phase, 'verified')

  const running = dailyVerificationState({ ...liveRun, status: 'RUNNING' }, true)
  assert.equal(running.phase, 'running')
  assert.equal(running.liveItems.length, 0)
  assert.equal(running.liveRecommendations.length, 0)

  const appSource = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8')
  const platformBrowserSource = await readFile(new URL('../../backend/app/platform_browser.py', import.meta.url), 'utf8')
  assert.match(appSource, /<RadarPage\s+key=\{workspaceId\}/, '工作区切换必须卸载旧雷达状态，旧批次和在途结果不能进入新工作区')
  assert.match(appSource, /existing\?\.archive_job\?\.status === 'PAUSED'[\s\S]*?await archiveXhsPost\(existing, true\)/, '已暂停原帖重试必须走显式恢复入口')
  assert.match(appSource, /COMPLETED: '候选采集已完成'/, '候选收齐不能显示为全部正文已完成')
  assert.match(appSource, /候选进度 \{batchProgress\}%/, '进度百分比必须明确仅计算搜索候选')
  assert.match(appSource, /当前没有运行中的归档任务/, '空闲队列应说明运行状态，不冒充正文成功')
  assert.doesNotMatch(appSource, /本轮小红书正文归档已处理/, '暂停或失败后的空闲队列不能展示成功文案')
  assert.match(appSource, /正文完整度以各条目的核验结果为准，暂停或失败的任务仍需处理/, '空闲提示必须保留逐条正文核验边界')
  assert.match(appSource, /dailyVerifiedItems\.map\(/, '每日实时结果必须完整遍历')
  assert.match(appSource, /topicSources\.map\(/, '主题搜索结果必须完整遍历')
  assert.doesNotMatch(appSource, /dailyVerifiedItems\.slice\(/, '每日结果不能静默截断')
  assert.doesNotMatch(appSource, /topicSources\.slice\(/, '主题搜索结果不能静默截断')
  assert.doesNotMatch(appSource, /只对 A 类候选逐篇/, '原文归档不能受 JEV 推荐分层限制')
  assert.match(appSource, /findXhsExtraction\(xhsExtractions, candidate\.url\)/, '批次候选卡必须关联同一帖子正文归档')
  assert.match(appSource, /PAGE_STRUCTURE_CHANGED/, '不支持的页面正文结构必须单独提示，不能泛化成正文为空')
  assert.match(appSource, /fullPublishedCopy \? '导出完整发布文案 \.txt' : '导出已读取片段 \.txt'/, '未核验完整的抖音搜索卡片文案必须明确作为片段导出')
  assert.match(appSource, /extraction\.video_id \? `抖音_\$\{extraction\.video_id\}`/, '抖音文案导出文件应使用视频 ID，不能把摘要正文当文件名')
  assert.match(appSource, /<pre className="xhs-reading-copy" aria-label="逐字原文">\{archive\.body_raw\}<\/pre>/, '小红书详情展示使用原始正文')
  assert.match(appSource, /copyPlainText\(archive\.body_raw \|\| ''\)/, '小红书复制使用同一原始正文')
  assert.match(appSource, /downloadPlainText\(extraction\.title \|\| '小红书正文', archive\.body_raw \|\| ''\)/, '小红书导出使用同一原始正文')
  assert.match(appSource, /<pre className="xhs-reading-copy">\{extraction\.published_body \|\| '页面未提供可读发布文案。'\}<\/pre>/, '抖音发布文案展示不得改用转写')
  assert.match(appSource, /搜索结果候选文本（不是详情页发布文案）/, '搜索候选文本必须与平台发布文案分开展示')
  assert.match(appSource, /!extraction\.observed_video_id \? '未核验'/, '没有页面内容证据时不能把视频 ID 标成已核验失败或成功')
  assert.match(appSource, /copyPlainText\(extraction\.published_body\)/, '抖音复制使用发布文案字段')
  assert.match(appSource, /downloadPlainText\([^\n]+, extraction\.published_body\)/, '抖音导出使用发布文案字段')
  assert.doesNotMatch(appSource, /downloadPlainText\([^\n]*extraction\.asr_text/, '口播转写不能被当作发布文案导出')
  assert.match(mainSource, /WorkbenchErrorBoundary/, '顶层错误边界必须存在，避免白屏')

  const tripleQuote = "'".repeat(3)
  const methodStart = platformBrowserSource.indexOf('def extract_douyin_detail')
  const expressionMarker = `expression = r${tripleQuote}`
  const expressionStart = platformBrowserSource.indexOf(expressionMarker, methodStart)
  assert.notEqual(methodStart, -1, 'Douyin detail parser must exist in the current backend')
  assert.notEqual(expressionStart, -1, 'Douyin detail JavaScript expression must exist')
  const expressionBodyStart = expressionStart + expressionMarker.length
  const expressionEnd = platformBrowserSource.indexOf(tripleQuote, expressionBodyStart)
  assert.notEqual(expressionEnd, -1, 'Douyin detail JavaScript expression must terminate')
  const douyinDetailExpression = platformBrowserSource.slice(expressionBodyStart, expressionEnd)
  const videoId = '7450123456789012345'
  const runDouyinDetailExpression = (items, route = 'video', metadata = {}) => {
    const window = { __INITIAL_STATE__: { items } }
    const document = {
      title: metadata.title || '', body: { innerText: '' },
      querySelector: selector => selector === 'meta[property="og:url"]' ? { content: metadata.ogUrl || '' }
        : selector === 'link[rel="canonical"]' ? { href: metadata.canonicalUrl || '' }
          : selector === 'meta[property="og:description"]' ? { content: metadata.ogDescription || '' }
            : selector === 'meta[name="description"]' ? { content: metadata.description || '' } : null,
      querySelectorAll: selector => selector.includes('script') ? (metadata.scripts || []) : [],
    }
    const location = { pathname: `/${route}/${videoId}`, href: `https://www.douyin.com/${route}/${videoId}` }
    return new Function('window', 'document', 'location', `return ${douyinDetailExpression}`)(window, document, location)
  }
  const fullerDuplicate = {
    aweme_id: videoId,
    desc: '这是一段完整的详情发布文案，包含多句正文和 #主题标签。'.repeat(3),
    video: {}, author: { nickname: '测试作者' }, statistics: { digg_count: 12 }, create_time: 1780000000,
  }
  const duplicateResult = runDouyinDetailExpression([
    { aweme_id: videoId, desc: '搜索卡片短摘要' },
    fullerDuplicate,
  ])
  assert.equal(duplicateResult.publishedBody, fullerDuplicate.desc, '同 ID 多对象时应选择较完整的详情文案')
  assert.equal(duplicateResult.structuredCandidateCount, 2)
  assert.equal(duplicateResult.selectedCandidateCaptionField, 'desc')

  const whitespaceFallback = runDouyinDetailExpression([
    { aweme_id: videoId, desc: '   ', description: 'desc 为空白时仍应读取备用发布文案' },
  ])
  assert.equal(whitespaceFallback.publishedBody, 'desc 为空白时仍应读取备用发布文案')
  assert.equal(whitespaceFallback.selectedCandidateCaptionField, 'description')

  const emptyShell = runDouyinDetailExpression([])
  assert.equal(emptyShell.observedVideoId, '', '请求的 URL ID 不能冒充页面实际呈现的视频 ID')
  assert.equal(emptyShell.pageHasDetailContent, false, '空壳页面必须可诊断为未加载详情内容')

  const noteRoute = runDouyinDetailExpression([], 'note', {
    title: '真实作品标题 - 抖音',
    ogUrl: `https://www.douyin.com/note/${videoId}`,
    ogDescription: '详情页元数据中的文案片段 #测试',
  })
  assert.equal(noteRoute.observedVideoId, videoId, '平台将 /video/ 重定向到 /note/ 时应从详情元数据核验同一作品 ID')
  assert.equal(noteRoute.publishedBody, '详情页元数据中的文案片段 #测试')
  assert.equal(noteRoute.captionSource, '详情页元数据文案片段')
  assert.notEqual(noteRoute.captionSource, '详情页结构化发布文案', '元数据片段不能冒充完整核验正文')
  assert.equal(noteRoute.title, '真实作品标题')

  const encodedRenderData = encodeURIComponent(JSON.stringify({
    aweme_detail: { aweme_id: videoId, desc: 'RENDER_DATA 编码字段中的原帖完整发布文案 #相亲' },
  }))
  const encodedDetail = runDouyinDetailExpression([], 'note', {
    scripts: [{ id: 'RENDER_DATA', type: 'application/json', textContent: encodedRenderData }],
  })
  assert.equal(encodedDetail.observedVideoId, videoId)
  assert.equal(encodedDetail.publishedBody, 'RENDER_DATA 编码字段中的原帖完整发布文案 #相亲')
  assert.equal(encodedDetail.captionSource, '详情页结构化发布文案')

  console.log('radar verification UI: passed')
} finally {
  await server.close()
}
