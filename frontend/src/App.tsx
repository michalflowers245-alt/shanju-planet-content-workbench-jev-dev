import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Archive, ArchiveRestore, ArrowDown, ArrowUpRight, Bot, CalendarDays, Camera, Check, CheckCircle2,
  ChevronDown, Copy, Download, ExternalLink, FileText, Heart, History, ImagePlus,
  LoaderCircle, NotebookPen, PenLine, RefreshCw, RotateCcw, Save, Search, Settings,
  ShieldCheck, Sparkles, Trash2, UserRoundSearch, WandSparkles, Wifi, WifiOff, X,
} from 'lucide-react'
import gatherLogo from './assets/gather-planet-logo.jpg'
import heartPlanet from './assets/heart-planet.png'
import { api, apiUrl, jsonBody } from './api'
import { canResumeRadarDetails, canRetryDouyinTranscript, collectorBrowserState, dailyVerificationState, douyinTranscriptQuality, hasRetainedDouyinCaption, isCompleteDouyinPublishedCopy, isPendingDouyinExtraction, isPendingXhsArchiveBatch, isPendingXhsExtraction, isRealtimeVerifiedPlatform, type DailyVerificationState } from './radarVerification'
import { copyPlainText, downloadPlainText } from './plainTextExport'
import type {
  Bootstrap, ContentType, DeepSeekSettings, Draft, DraftStrategy, JevSettings, LifeCase, LifeCaseVariant, LocalProxySettings, ProductDeliverables,
  Review, SkillStatus, Source, Task, TaskDetail, WorkspaceProfile,
  TopicMonitor, TopicRadarItem, TopicRadarRecommendation, TopicRadarRun, TopicSearchResponse,
  PlatformSearchLink, PlatformBrowserStatus, XiaohongshuExtraction, XiaohongshuArchive, XiaohongshuArchiveBatch,
  DouyinExtraction, RadarBatchCandidate, RadarBatchCandidatePage, RadarFavorite, RadarPersona, RadarSearchBatch,
  RewriteJob, RewriteStage,
} from './types'

type Section = 'create' | 'radar' | 'works' | 'settings'
type Notice = { text: string; kind: 'ok' | 'error' } | null
type ProductMode = 'moments_ops' | 'xiaohongshu_ip' | 'douyin_emotion' | 'moments_life_case'
type RadarSeed = {
  topic: string
  sourceUrls: string[]
  searchQuery?: string
  platformSearches?: PlatformSearchLink[]
}

const productModes = [
  {
    key: 'moments_ops' as ProductMode,
    title: '朋友圈自动运营',
    short: '朋友圈运营',
    description: '活动预热、日常分享，把信任写进每一天。',
    badge: '日常陪伴',
    placeholder: '输入这轮朋友圈要围绕的主题，例如：让本地客户逐步了解我的相亲服务，但不要每天都在硬卖。',
    contentType: 'moments',
    platform: '微信朋友圈',
    action: '生成朋友圈运营计划',
    examples: [
      '周末脱单局，提前 7 天温柔预热', '聊聊第一次参加交友活动的紧张', '让同城朋友慢慢了解我的相亲服务',
      '活动结束后，一个让我印象很深的小细节', '今天遇到一位认真对待感情的女生', '为什么我们坚持先了解需求再推荐对象',
      '分享一次线下交友活动的准备过程', '圈子小的人，也值得被认真介绍', '相亲前最应该想清楚的一个问题',
      '这周收到的一句真实反馈', '红娘工作里看见的温柔瞬间', '给第一次来参加活动的人一点安心',
      '为什么真实资料比漂亮话更重要', '周末活动名额提醒，但不想写得像广告', '今天想聊聊慢慢认识一个人的意义',
    ],
    icon: CalendarDays,
    tone: 'green',
  },
  {
    key: 'xiaohongshu_ip' as ProductMode,
    title: '小红书 IP 文案',
    short: '小红书文案',
    description: '讲清你的态度，让同频的人先看见你。',
    badge: '同城吸引',
    placeholder: '输入一个主题，例如：第一次参加线下相亲，女生最容易忽略的三个判断细节。',
    contentType: 'xiaohongshu',
    platform: '小红书',
    action: '生成小红书发布稿',
    examples: [
      '第一次线下相亲，女生最容易忽略的 3 个细节', '圈子小的女生，怎么认识更合适的人', '做相亲平台后，我发现的一个真实现象',
      '长沙女生扩大异性圈，可以先做这 4 件事', '相亲资料里，哪些信息真的值得认真看', '为什么条件不错的人也会一直单身',
      '第一次参加线下交友活动是什么体验', '女生主动认识异性，并不等于降低标准', '相亲见面前，先聊清楚这几个问题',
      '红娘眼里更容易脱单的人有什么共同点', '圈子固定以后，怎样自然认识新朋友', '别急着看条件，相处舒服要看这些细节',
      '同城相亲活动怎么选，才不会浪费周末', '认真脱单的人，正在悄悄改变哪些习惯', '相亲后对方没有回复，应该怎么判断',
    ],
    icon: NotebookPen,
    tone: 'red',
  },
  {
    key: 'douyin_emotion' as ProductMode,
    title: '抖音情感文案',
    short: '抖音情感',
    description: '把恋爱里的小情绪，写成听得进去的口播。',
    badge: '情感共鸣',
    placeholder: '输入一个情感主题，例如：成年人的离开，往往不是一次争吵，而是很多次失望都没说出口。',
    contentType: 'short_video',
    platform: '抖音',
    action: '生成抖音情感口播',
    examples: [
      '成年人真正的失望，往往很安静', '真正的离开，是从不再解释开始', '被好好爱过的人，是什么样子',
      '真正想见你的人，不会一直说下次', '关系里最让人心寒的不是争吵', '你舍不得的，也许只是曾经的期待',
      '爱你的人会认真回应你的情绪', '停止内耗，是从不再猜他开始', '有些人错过以后，才懂得珍惜',
      '成年人的喜欢，藏在一次次行动里', '一段关系开始变淡，会有哪些信号', '好的感情，不需要你反复证明自己',
      '为什么越懂事的人越容易受委屈', '真正的告别，往往没有正式说再见', '遇见对的人以后，你会慢慢放松下来',
    ],
    icon: Heart,
    tone: 'purple',
  },
  {
    key: 'moments_life_case' as ProductMode,
    title: '生活案例朋友圈',
    short: '生活案例',
    description: '留住照片和小事，让真实的你更有温度。',
    badge: '真实瞬间',
    placeholder: '写下发生了什么、你当时注意到什么，以及哪些细节一定不能写错。',
    contentType: 'moments',
    platform: '微信朋友圈',
    action: '保存案例并生成三版',
    examples: [],
    icon: Camera,
    tone: 'amber',
  },
] as const

const terminalStatuses = new Set(['FINAL_READY', 'NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED', 'CANCELLED'])
const employeeStages = [
  { key: 'RESEARCHER', title: '研究员', description: '搜索资料与客户问题', icon: UserRoundSearch },
  { key: 'WRITER', title: '文案员工', description: '完成标题与正文初稿', icon: PenLine },
  { key: 'EDITOR', title: '主编', description: '核证据、改表达、定稿', icon: ShieldCheck },
] as const

const statusNames: Record<string, string> = {
  QUEUED: '等待处理',
  RUNNING: '正在创作',
  FINAL_READY: '已定稿',
  NEEDS_MODEL: '需要连接模型',
  NEEDS_ATTENTION: '需要处理',
  FAILED: '未完成',
  CANCELLED: '已取消',
}

function normalizeStage(stage: string) {
  if (['RESEARCHER', 'RESEARCHING'].includes(stage)) return 'RESEARCHER'
  if (['WRITER', 'WRITING', 'WRITING_V1', 'STRATEGIZING', 'REVISING'].includes(stage)) return 'WRITER'
  if (['EDITOR', 'EDITING', 'EXPRESSION_CHECK', 'HUMANIZING'].includes(stage)) return 'EDITOR'
  if (['FINAL', 'FINAL_READY'].includes(stage)) return 'FINAL'
  return 'RESEARCHER'
}

function formatDate(value?: string) {
  if (!value) return ''
  const raw = String(value).trim()
  const numeric = /^\d{10,13}$/.test(raw) ? Number(raw) : Number.NaN
  const date = Number.isFinite(numeric)
    ? new Date(raw.length === 10 ? numeric * 1000 : numeric)
    : new Date(raw)
  if (Number.isNaN(date.getTime())) return raw.slice(0, 24)
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
  } catch {
    return raw.slice(0, 24)
  }
}

function attemptTimeLabel(value?: string, status?: string) {
  if (value) return formatDate(value)
  return ['queued', 'extracting', 'ready_to_archive', 'pending', 'QUEUED', 'TRANSCRIBING'].includes(status || '')
    ? '尚未尝试'
    : '历史记录无精确时间'
}

function taskProductLabel(task: Task) {
  const mode = String(task.constraints?.product_mode || '')
  if (mode === 'moments_ops') return '朋友圈自动运营'
  if (mode === 'xiaohongshu_ip') return '小红书 IP 文案'
  if (mode === 'douyin_emotion') return '抖音情感文案'
  if (mode === 'moments_life_case') return '生活案例朋友圈'
  return task.platform
}

function localToday() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function momentsPostsFromBody(body: string, fallback: ProductDeliverables['moments_posts']) {
  const sections = body.split(/\n\s*---\s*\n/).map(item => item.trim()).filter(Boolean)
  if (sections.length !== fallback.length) return []
  return sections.map((section, index) => {
    const lines = section.split(/\r?\n/)
    const heading = lines.shift() || ''
    const metadata = (lines.shift() || '').split('｜')
    const rest = lines.join('\n').trim()
    const parts = rest.split(/\n\s*互动承接[：:]\s*/)
    const dayMatch = heading.match(/第\s*(\d+)\s*天/)
    const timeMatch = heading.match(/[·｜]\s*([^\n]+)$/)
    return {
      day: Number(dayMatch?.[1] || fallback[index].day || index + 1),
      time: timeMatch?.[1]?.trim() || fallback[index].time,
      role: metadata[0]?.trim() || fallback[index].role,
      purpose: metadata[1]?.trim() || fallback[index].purpose,
      content: parts[0]?.trim() || fallback[index].content,
      follow_up: parts.slice(1).join('\n').trim() || fallback[index].follow_up,
    }
  })
}

function App() {
  const [section, setSection] = useState<Section>('create')
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [workspaceId, setWorkspaceId] = useState('')
  const [tasks, setTasks] = useState<Task[]>([])
  const [lifeCases, setLifeCases] = useState<LifeCase[]>([])
  const [lifeCaseSeed, setLifeCaseSeed] = useState<LifeCase | null>(null)
  const [radarSeed, setRadarSeed] = useState<RadarSeed | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [notice, setNotice] = useState<Notice>(null)

  const notify = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    setNotice({ text, kind })
    window.setTimeout(() => setNotice(null), 3600)
  }, [])

  const loadBootstrap = useCallback(async () => {
    const data = await api<Bootstrap>('/api/bootstrap')
    setBootstrap(data)
    setWorkspaceId(current => current || data.workspaces[0]?.id || '')
    return data
  }, [])

  const loadTasks = useCallback(async (id: string) => {
    if (!id) return
    setTasks(await api<Task[]>(`/api/copy-tasks?workspace_id=${encodeURIComponent(id)}`))
  }, [])

  const loadLifeCases = useCallback(async (id: string) => {
    if (!id) return
    setLifeCases(await api<LifeCase[]>(`/api/life-cases?workspace_id=${encodeURIComponent(id)}&include_archived=true`))
  }, [])

  const loadDetail = useCallback(async (id: string) => {
    const value = await api<TaskDetail>(`/api/copy-tasks/${id}`)
    setDetail(value)
    return value
  }, [])

  useEffect(() => {
    loadBootstrap().catch(error => notify((error as Error).message, 'error'))
  }, [loadBootstrap, notify])

  useEffect(() => {
    if (!workspaceId) return
    setDetail(null)
    Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]).catch(error => notify((error as Error).message, 'error'))
  }, [workspaceId, loadLifeCases, loadTasks, notify])

  useEffect(() => {
    if (!detail || terminalStatuses.has(detail.task.status)) return
    const timer = window.setInterval(async () => {
      try {
        const next = await loadDetail(detail.task.id)
        if (terminalStatuses.has(next.task.status)) {
          await Promise.all([loadTasks(next.task.workspace_id), loadLifeCases(next.task.workspace_id)])
          await loadBootstrap()
        }
      } catch (error) {
        notify((error as Error).message, 'error')
      }
    }, 1400)
    return () => window.clearInterval(timer)
  }, [detail?.task.id, detail?.task.status, loadBootstrap, loadDetail, loadLifeCases, loadTasks, notify])

  const workspace = bootstrap?.workspaces.find(item => item.id === workspaceId)
  const activeProviderLabel = bootstrap?.model_status.active_label || '模型服务'
  const openTask = async (taskId: string, target: Section = 'works') => {
    setSection(target)
    await loadDetail(taskId)
  }

  if (!bootstrap) {
    return <div className="app-loading"><LoaderCircle className="spin" /><span>正在打开文案工作台</span></div>
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => setSection('create')} aria-label="回到开始创作">
          <span className="brand-logo"><img src={gatherLogo} alt="闪聚星球 GATHER PLANET" width="488" height="180" /></span>
          <span className="brand-descriptor">内容工作室</span>
        </button>
        <nav className="main-nav" aria-label="主导航">
          <NavButton active={section === 'create'} icon={WandSparkles} label="开始创作" onClick={() => setSection('create')} />
          <NavButton active={section === 'radar'} icon={Search} label="内容雷达" onClick={() => setSection('radar')} />
          <NavButton active={section === 'works'} icon={History} label="作品记录" onClick={() => setSection('works')} />
          <NavButton active={section === 'settings'} icon={Settings} label="设置" onClick={() => setSection('settings')} />
        </nav>
        <div className="top-actions">
          {bootstrap.workspaces.length > 1 ? (
            <select className="workspace-select" value={workspaceId} onChange={event => setWorkspaceId(event.target.value)}>
              {bootstrap.workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          ) : <span className="workspace-name">{workspace?.name === '我的品牌工作区' ? '闪聚创作空间' : workspace?.name || '闪聚创作空间'}</span>}
          <button
            className={`connection-pill ${bootstrap.model_status.usable ? 'online' : 'offline'}`}
            onClick={() => setSection('settings')}
          >
            {bootstrap.model_status.usable ? <Wifi size={15} /> : <WifiOff size={15} />}
            {bootstrap.model_status.usable ? `${activeProviderLabel} 已连接` : `连接${activeProviderLabel}`}
          </button>
        </div>
      </header>

      <main className="page-shell">
        <div hidden={section !== 'create'}>
          <CreatePage
            key={workspaceId}
            workspaceId={workspaceId}
            modelUsable={bootstrap.model_status.usable}
            lifeCaseSeed={lifeCaseSeed}
            radarSeed={radarSeed}
            detail={section === 'create' ? detail : null}
            onCreated={async value => {
              setDetail(value)
              await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)])
            }}
            onSeedConsumed={() => setLifeCaseSeed(null)}
            onRadarSeedConsumed={() => setRadarSeed(null)}
            onCasesChanged={() => loadLifeCases(workspaceId)}
            onOpenSettings={() => setSection('settings')}
            onRefresh={async () => { if (detail) await loadDetail(detail.task.id) }}
            notify={notify}
          />
        </div>
        {section === 'radar' && (
          <RadarPage
            key={workspaceId}
            workspaceId={workspaceId}
            onCreateFromTopic={seed => {
              setLifeCaseSeed(null)
              setRadarSeed(seed)
              setSection('create')
              notify(`已带入创作台：${seed.topic}`)
            }}
            onTaskCreated={async value => {
              setLifeCaseSeed(null)
              setRadarSeed(null)
              setDetail(value)
              await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)])
              setSection('create')
            }}
            notify={notify}
          />
        )}
        {section === 'works' && (
          <WorksPage
            tasks={tasks}
            lifeCases={lifeCases}
            detail={detail}
            contentTypes={bootstrap.content_types}
            onOpen={id => openTask(id)}
            onRefresh={async () => { if (detail) await loadDetail(detail.task.id); await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]) }}
            onCreated={async value => { setDetail(value); await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]) }}
            onCasesChanged={() => loadLifeCases(workspaceId)}
            onDuplicateCase={value => { setDetail(null); setLifeCaseSeed(value); setSection('create') }}
            onSettings={() => setSection('settings')}
            notify={notify}
          />
        )}
        {section === 'settings' && (
          <SettingsPage
            workspaceId={workspaceId}
            modelStatus={bootstrap.model_status}
            skills={bootstrap.skills}
            onUpdated={loadBootstrap}
            notify={notify}
          />
        )}
      </main>

      <nav className="mobile-nav" aria-label="手机导航">
        <NavButton active={section === 'create'} icon={WandSparkles} label="创作" onClick={() => setSection('create')} />
        <NavButton active={section === 'radar'} icon={Search} label="雷达" onClick={() => setSection('radar')} />
        <NavButton active={section === 'works'} icon={History} label="作品" onClick={() => setSection('works')} />
        <NavButton active={section === 'settings'} icon={Settings} label="设置" onClick={() => setSection('settings')} />
      </nav>
      {notice && <div className={`toast ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.kind === 'ok' ? <Check size={16} /> : <AlertTriangle size={16} />}{notice.text}</div>}
    </div>
  )
}

function NavButton({ active, icon: Icon, label, onClick }: { active: boolean; icon: typeof Settings; label: string; onClick: () => void }) {
  return <button className={active ? 'active' : ''} aria-current={active ? 'page' : undefined} onClick={onClick}><Icon size={17} /><span>{label}</span></button>
}

function CreatePage({
  workspaceId, modelUsable, lifeCaseSeed, radarSeed, detail, onCreated, onSeedConsumed, onRadarSeedConsumed, onCasesChanged,
  onOpenSettings, onRefresh, notify,
}: {
  workspaceId: string
  modelUsable: boolean
  lifeCaseSeed: LifeCase | null
  radarSeed: RadarSeed | null
  detail: TaskDetail | null
  onCreated: (value: TaskDetail) => Promise<void>
  onSeedConsumed: () => void
  onRadarSeedConsumed: () => void
  onCasesChanged: () => Promise<void>
  onOpenSettings: () => void
  onRefresh: () => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [productMode, setProductMode] = useState<ProductMode>('moments_ops')
  const [inspirationOffsets, setInspirationOffsets] = useState<Partial<Record<ProductMode, number>>>({})
  const [briefs, setBriefs] = useState<Partial<Record<ProductMode, string>>>({})
  const brief = briefs[productMode] || ''
  const setBrief = (value: string) => setBriefs(current => ({ ...current, [productMode]: value }))
  const selectedMode = productModes.find(item => item.key === productMode) || productModes[0]
  const inspirationExamples = useMemo(() => {
    const examples = [...selectedMode.examples]
    if (!examples.length) return []
    const offset = (inspirationOffsets[productMode] || 0) % examples.length
    return Array.from({ length: Math.min(3, examples.length) }, (_, index) => examples[(offset + index) % examples.length])
  }, [inspirationOffsets, productMode, selectedMode.examples])
  const [goal, setGoal] = useState('')
  const [offer, setOffer] = useState('')
  const [sourceUrls, setSourceUrls] = useState('')
  const [radarContext, setRadarContext] = useState<Pick<RadarSeed, 'searchQuery' | 'platformSearches'> | null>(null)
  const [campaignDays, setCampaignDays] = useState('7')
  const [campaignGoal, setCampaignGoal] = useState('建立信任')
  const [xhsGoal, setXhsGoal] = useState('同步 IP 并获客')
  const [syncIp, setSyncIp] = useState(true)
  const [emotion, setEmotion] = useState('共鸣')
  const [duration, setDuration] = useState('60')
  const [lifeNote, setLifeNote] = useState('')
  const [lifeDate, setLifeDate] = useState(localToday())
  const [lifeSyncIp, setLifeSyncIp] = useState(true)
  const [lifeFiles, setLifeFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const composerRef = useRef<HTMLElement>(null)

  const selectMode = (mode: ProductMode) => {
    setProductMode(mode)
    if (window.matchMedia('(max-width: 640px)').matches) {
      window.requestAnimationFrame(() => composerRef.current?.scrollIntoView({
        block: 'start',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
      }))
    }
  }


  const refreshInspirations = () => {
    const total = selectedMode.examples.length
    if (!total) return
    setInspirationOffsets(current => ({
      ...current,
      [productMode]: ((current[productMode] || 0) + 3) % total,
    }))
  }

  const lifePreviews = useMemo(() => lifeFiles.map(file => ({ file, url: URL.createObjectURL(file) })), [lifeFiles])

  useEffect(() => () => lifePreviews.forEach(item => URL.revokeObjectURL(item.url)), [lifePreviews])

  useEffect(() => {
    if (!lifeCaseSeed) return
    let cancelled = false
    const seed = async () => {
      try {
        const files = await Promise.all(lifeCaseSeed.media
          .slice()
          .sort((a, b) => a.ordinal - b.ordinal)
          .map(async media => {
            const response = await fetch(apiUrl(media.media_url))
            if (!response.ok) throw new Error(`读取图片失败（${response.status}）`)
            const blob = await response.blob()
            return new File([blob], media.display_name, { type: media.mime_type || blob.type })
          }))
        if (cancelled) return
        setProductMode('moments_life_case')
        setLifeNote(lifeCaseSeed.note_text)
        setLifeDate(lifeCaseSeed.occurred_at?.slice(0, 10) || localToday())
        setLifeSyncIp(lifeCaseSeed.sync_ip)
        setLifeFiles(files)
        notify('案例已复制，请修改后保存为新案例')
      } catch (error) {
        notify((error as Error).message, 'error')
      } finally {
        if (!cancelled) onSeedConsumed()
      }
    }
    seed()
    return () => { cancelled = true }
  }, [lifeCaseSeed, notify, onSeedConsumed])

  useEffect(() => {
    if (!radarSeed) return
    setProductMode('xiaohongshu_ip')
    setBriefs(current => ({ ...current, xiaohongshu_ip: radarSeed.topic }))
    setSourceUrls(radarSeed.sourceUrls.join('\n'))
    setRadarContext({ searchQuery: radarSeed.searchQuery, platformSearches: radarSeed.platformSearches })
    onRadarSeedConsumed()
    window.requestAnimationFrame(() => composerRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }, [onRadarSeedConsumed, radarSeed])

  const addLifeFiles = (incoming: File[]) => {
    const acceptedExtensions = /\.(jpe?g|png|webp|gif|heic|heif)$/i
    const valid = incoming.filter(file => {
      if (!acceptedExtensions.test(file.name)) {
        notify(`${file.name} 不是支持的图片格式`, 'error')
        return false
      }
      if (file.size > 12 * 1024 * 1024) {
        notify(`${file.name} 超过 12MB`, 'error')
        return false
      }
      return true
    })
    const next = [...lifeFiles, ...valid]
    if (next.length > 9) {
      notify('一次最多上传 9 张图片', 'error')
      return
    }
    if (next.reduce((sum, file) => sum + file.size, 0) > 30 * 1024 * 1024) {
      notify('图片总大小不能超过 30MB', 'error')
      return
    }
    setLifeFiles(next)
  }

  const submitLifeCase = async () => {
    if (!lifeNote.trim()) {
      notify('请写下这件小事的经过或你当时的感受', 'error')
      return
    }
    if (lifeFiles.length === 0) {
      notify('请至少上传 1 张生活图片', 'error')
      return
    }
    setSubmitting(true)
    let saved = false
    try {
      const form = new FormData()
      form.append('workspace_id', workspaceId)
      form.append('note_text', lifeNote.trim())
      form.append('occurred_at', lifeDate)
      form.append('sync_ip', String(lifeSyncIp))
      lifeFiles.forEach(file => form.append('images', file, file.name))
      const lifeCase = await api<LifeCase>('/api/life-cases', { method: 'POST', body: form })
      saved = true
      setLifeNote('')
      setLifeFiles([])
      setLifeDate(localToday())
      await onCasesChanged()
      const value = await api<TaskDetail>(`/api/life-cases/${lifeCase.id}/generate?workspace_id=${encodeURIComponent(lifeCase.workspace_id)}`, { method: 'POST' })
      await onCreated(value)
      notify(modelUsable ? '案例已保存，三名数字员工已经开始工作' : '案例已保存，连接模型后可继续生成')
    } catch (error) {
      notify(saved ? `案例已保存；生成暂未开始：${(error as Error).message}` : (error as Error).message, 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const submit = async () => {
    if (productMode === 'moments_life_case') {
      await submitLifeCase()
      return
    }
    if (!modelUsable) {
      onOpenSettings()
      notify('请先在设置中测试并启用一个模型', 'error')
      return
    }
    if (brief.trim().length < 2) {
      notify('先告诉数字员工你想写什么', 'error')
      return
    }
    setSubmitting(true)
    try {
      const constraints: Record<string, unknown> = { product_mode: productMode }
      if (radarContext?.searchQuery || radarContext?.platformSearches?.length) {
        constraints.radar_origin = 'ip_daily'
        if (radarContext.searchQuery) constraints.radar_search_query = radarContext.searchQuery
        if (radarContext.platformSearches?.length) constraints.radar_platform_searches = radarContext.platformSearches
      }
      let defaultGoal = '获取精准咨询'
      if (productMode === 'moments_ops') {
        const days = Number(campaignDays)
        constraints.campaign_days = days
        constraints.campaign_goal = campaignGoal
        constraints.target_length = [Math.max(500, days * 120), Math.min(20000, days * 360)]
        defaultGoal = campaignGoal
      } else if (productMode === 'xiaohongshu_ip') {
        constraints.content_goal = xhsGoal
        constraints.sync_ip = syncIp
        constraints.target_length = [500, 1200]
        defaultGoal = xhsGoal
      } else {
        const seconds = Number(duration)
        constraints.emotion = emotion
        constraints.duration_seconds = seconds
        constraints.target_length = [Math.max(120, seconds * 4), Math.max(220, seconds * 7)]
        defaultGoal = '引发真实情绪共鸣和互动'
      }
      const value = await api<TaskDetail>('/api/copy-tasks', jsonBody('POST', {
        workspace_id: workspaceId,
        content_type: selectedMode.contentType,
        platform: selectedMode.platform,
        topic: brief.trim(),
        goal: goal.trim() || defaultGoal,
        offer: offer.trim(),
        constraints,
        source_urls: sourceUrls.split(/\r?\n/).map(item => item.trim()).filter(Boolean),
        web_research: true,
      }))
      await onCreated(value)
      notify('三名数字员工已经开始工作')
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="create-page">
      <section className="hero">
        <div className="hero-copy">
          <span className="studio-label"><span />GATHER PLANET · 闪聚星球</span>
          <h1>写点心动，<br /><em>让缘分发生。</em><Sparkles aria-hidden="true" /></h1>
          <p>把相遇的故事、恋爱的感悟、活动的小美好，<br className="desktop-break" />写成让人想靠近的内容。你的灵感，交给闪聚。</p>
          <div className="hero-topics"><span><Heart />情感共鸣</span><i /><span>同城相遇</span><i /><span>真实故事</span></div>
        </div>
        <div className="hero-scene" aria-hidden="true">
          <div className="scene-art"><img src={heartPlanet} alt="" width="1254" height="1254" fetchPriority="high" /></div>
          <span className="scene-note note-top"><Sparkles />今天，灵感也心动了</span>
          <span className="scene-note note-bottom"><Heart />让同频的人，看见你<span>♡</span></span>
          <span className="scene-spark spark-one">✳</span><span className="scene-spark spark-two">✧</span>
        </div>
      </section>

      <div className="creation-heading"><div><span className="section-number">01</span><h2>选一个心动入口</h2><p>今天，想怎样被看见？</p></div><span className="section-aside">让好内容，成为相遇的开始 <ArrowDown /></span></div>
      <section className="product-switcher" aria-label="内容工作台">
        {productModes.map(mode => {
          const Icon = mode.icon
          return (
            <button
              key={mode.key}
              className={`${productMode === mode.key ? 'active' : ''} ${mode.tone}`}
              onClick={() => selectMode(mode.key)}
              aria-pressed={productMode === mode.key}
            >
              <span className="mode-icon"><Icon /></span>
              <span className="mode-badge">{mode.badge}</span>
              <div><strong>{mode.title}</strong><small>{mode.description}</small></div>
              <span className="mode-state">{productMode === mode.key ? <><CheckCircle2 />已选择</> : <>开始创作<ArrowUpRight /></>}</span>
            </button>
          )
        })}
      </section>

      <div className="studio-workspace">
      <section className={`composer-card mode-${selectedMode.tone}`} ref={composerRef}>
        <div className="composer-mode-title"><span className="section-number">02</span><div><strong>{productMode === 'moments_life_case' ? '把生活里的小美好，留在这里' : '说说你今天想分享什么'}</strong><span>{selectedMode.title} · 写下想法，剩下的我们来</span></div><span className="composer-step"><PenLine />灵感起笔</span></div>
        {productMode === 'moments_life_case' ? (
          <div className="life-case-form">
            <label
              className="life-upload"
              onDragOver={event => event.preventDefault()}
              onDrop={event => {
                event.preventDefault()
                addLifeFiles(Array.from(event.dataTransfer.files))
              }}
            >
              <input
                type="file"
                multiple
                accept=".jpg,.jpeg,.png,.webp,.gif,.heic,.heif,image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif"
                onChange={event => {
                  addLifeFiles(Array.from(event.target.files || []))
                  event.target.value = ''
                }}
              />
              <span><ImagePlus /></span>
              <div><strong>上传生活图片</strong><small>点击选择或拖进来，1–9 张</small></div>
              <em>JPG、PNG、WebP、GIF、HEIC</em>
            </label>
            {lifePreviews.length > 0 && (
              <div className="life-preview-grid" aria-label="已选择的生活图片">
                {lifePreviews.map((item, index) => {
                  const heic = /\.hei[cf]$/i.test(item.file.name)
                  return (
                    <figure key={`${item.file.name}-${item.file.lastModified}-${index}`}>
                      {heic ? <div className="heic-placeholder"><Camera /><span>HEIC</span><small>{item.file.name}</small></div> : <img src={item.url} alt={`生活图片 ${index + 1}`} />}
                      <figcaption>{index + 1}</figcaption>
                      <button type="button" aria-label={`移除第 ${index + 1} 张图片`} onClick={() => setLifeFiles(files => files.filter((_, fileIndex) => fileIndex !== index))}><X /></button>
                    </figure>
                  )
                })}
              </div>
            )}
            <label className="life-note-field">
              <span>这件小事里发生了什么</span>
              <textarea value={lifeNote} onChange={event => setLifeNote(event.target.value)} placeholder={selectedMode.placeholder} maxLength={5000} />
              <small>{lifeNote.length}/5000 · 写清真实细节，数字员工不会替你编故事</small>
            </label>
            <div className="life-case-controls">
              <label><span>发生日期</span><input type="date" value={lifeDate} onChange={event => setLifeDate(event.target.value)} /></label>
              <label className="compact-toggle"><input type="checkbox" checked={lifeSyncIp} onChange={event => setLifeSyncIp(event.target.checked)} /><span className="switch" /><span>同步我的 IP 语气</span></label>
            </div>
          </div>
        ) : <>
          <div className="brief-field">
            <textarea
              className="brief-input"
              aria-label="创作主题"
              value={brief}
              onChange={event => setBrief(event.target.value)}
              placeholder={selectedMode.placeholder}
              maxLength={5000}
            />
            <div className="brief-helper">
              <div className="inspiration-chips" aria-label="灵感快捷填入">
                <span><Sparkles />没灵感？试试</span>
                <button className="inspiration-refresh" type="button" onClick={refreshInspirations} aria-label="换一批创作灵感"><RefreshCw />换一批</button>
                {inspirationExamples.map(example => (
                  <button type="button" key={example} onClick={() => setBrief(example)}>{example}</button>
                ))}
              </div>
              <div className="brief-count">
                {brief && <button type="button" onClick={() => setBrief('')} aria-label="清空主题"><X />清空</button>}
                <span>{brief.length}/5000</span>
              </div>
            </div>
          </div>
          <div className="mode-controls">
            {productMode === 'moments_ops' && <>
              <label><span>运营周期</span><select value={campaignDays} onChange={event => setCampaignDays(event.target.value)}><option value="3">连续 3 天</option><option value="7">连续 7 天</option><option value="14">连续 14 天</option></select></label>
              <label><span>这轮目标</span><select value={campaignGoal} onChange={event => setCampaignGoal(event.target.value)}><option>建立信任</option><option>日常活跃</option><option>活动预热</option><option>自然成交</option></select></label>
            </>}
            {productMode === 'xiaohongshu_ip' && <>
              <label><span>内容目标</span><select value={xhsGoal} onChange={event => setXhsGoal(event.target.value)}><option>同步 IP 并获客</option><option>经验分享</option><option>产品种草</option><option>建立专业感</option></select></label>
              <label className="compact-toggle"><input type="checkbox" checked={syncIp} onChange={event => setSyncIp(event.target.checked)} /><span className="switch" /><span>同步我的 IP 信息</span></label>
            </>}
            {productMode === 'douyin_emotion' && <>
              <label><span>情绪方向</span><select value={emotion} onChange={event => setEmotion(event.target.value)}><option>共鸣</option><option>治愈</option><option>遗憾</option><option>成长</option><option>关系清醒</option></select></label>
              <label><span>口播时长</span><select value={duration} onChange={event => setDuration(event.target.value)}><option value="30">约 30 秒</option><option value="60">约 60 秒</option><option value="90">约 90 秒</option></select></label>
            </>}
            <div className="search-toggle" aria-label="自动联网研究"><Search size={15} /><span>联网研究 · 自动执行</span></div>
          </div>
          <details className="more-options">
            <summary><ChevronDown size={16} />更多要求（选填）</summary>
            <div className="option-grid">
              <label><span>希望读者最后做什么</span><input value={goal} onChange={event => setGoal(event.target.value)} placeholder="不填则按当前模式自动决定" /></label>
              <label><span>承接的产品或服务</span><input value={offer} onChange={event => setOffer(event.target.value)} placeholder="没有可以留空" /></label>
              <label className="wide"><span>参考网址（每行一个）</span><textarea value={sourceUrls} onChange={event => setSourceUrls(event.target.value)} placeholder="https://..." /></label>
            </div>
          </details>
        </>}
        <div className="composer-footer">
          <span><ShieldCheck />{productMode === 'moments_life_case' ? '保留真实细节，生成三种表达供你挑选' : productMode === 'xiaohongshu_ip' && syncIp ? '联网研究主题、受众与业务，再融合你的 IP 信息和真实表达习惯' : '自动从主题、受众和业务角度联网研究；来源不足时明确保留待核实边界'}</span>
          <button className="primary-button" disabled={submitting} onClick={submit}>
            {submitting ? <LoaderCircle className="spin" /> : <Sparkles />}
            {submitting ? '正在交给数字员工' : selectedMode.action}
          </button>
        </div>
      </section>
      <aside className="studio-companion">
        <div className="companion-heading"><span><Sparkles /></span><small>你的创作搭子</small><h3>有想法就好，<br />我们帮你写出来。</h3></div>
        <div className="companion-steps">
          <div><span><Search /></span><div><strong>找一点灵感</strong><small>整理主题、参考与真实来源</small></div><i>01</i></div>
          <div><span><PenLine /></span><div><strong>写一点心动</strong><small>把想说的话，写得自然动人</small></div><i>02</i></div>
          <div><span><CheckCircle2 /></span><div><strong>再认真读一遍</strong><small>核对细节，让表达更像你</small></div><i>03</i></div>
        </div>
        <div className="companion-tip"><Heart /><p>好的相遇，从真实开始。<br />多写一点具体的小事，会更打动人。</p></div>
        <span className="companion-signature">MADE WITH LOVE, BY GATHER PLANET</span>
      </aside>
      </div>

      <div className="studio-signoff"><span />用真实表达，连接同频的人。<Heart /><span /></div>

      {detail && (
        <TaskWorkspace
          detail={detail}
          onRefresh={onRefresh}
          onSettings={onOpenSettings}
          notify={notify}
        />
      )}
    </div>
  )
}

function TaskWorkspace({ detail, onRefresh, onSettings, notify, showSourceCase = true }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
  showSourceCase?: boolean
}) {
  const current = normalizeStage(detail.task.stage)
  const currentIndex = current === 'FINAL' ? 3 : employeeStages.findIndex(item => item.key === current)
  const isProblem = ['NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED'].includes(detail.task.status)
  const lifeMode = detail.task.constraints?.product_mode === 'moments_life_case'
  const lifeStageDescriptions: Record<string, string> = {
    RESEARCHER: '识别图片与真实细节',
    WRITER: '写出三种朋友圈角度',
    EDITOR: '核事实、去 AI 味、定稿',
  }
  const lastMessages = new Map<string, string>()
  for (const event of detail.events) lastMessages.set(normalizeStage(event.stage), event.message)

  return (
    <section className="task-workspace">
      <div className="task-heading">
        <div><span className="eyebrow">当前任务</span><h2>{detail.task.topic}</h2></div>
        <span className={`status-badge ${detail.task.status.toLowerCase()}`}>{statusNames[detail.task.status] || detail.task.status}</span>
      </div>
      {showSourceCase && detail.source_case && <LifeCaseMaterial caseItem={detail.source_case} compact />}
      <div className="employee-flow">
        {employeeStages.map((employee, index) => {
          const done = detail.task.status === 'FINAL_READY' || currentIndex > index
          const active = currentIndex === index && detail.task.status !== 'FINAL_READY'
          const problem = active && isProblem
          return (
            <div className={`employee-card ${done ? 'done' : ''} ${active ? 'active' : ''} ${problem ? 'problem' : ''}`} key={employee.key}>
              <span className="employee-icon">{done ? <CheckCircle2 /> : problem ? <AlertTriangle /> : active ? <LoaderCircle className="spin" /> : <employee.icon />}</span>
              <div><strong>{employee.title}</strong><small>{lastMessages.get(employee.key) || (lifeMode ? lifeStageDescriptions[employee.key] : employee.description)}</small></div>
              {index < 2 && <i />}
            </div>
          )
        })}
      </div>
      {detail.task.revision_round > 0 && <div className="revision-note"><RotateCcw size={15} />主编已退回修改 {detail.task.revision_round} 次，本轮不会推翻整篇。</div>}
      {isProblem && <ProblemPanel detail={detail} onRefresh={onRefresh} onSettings={onSettings} notify={notify} />}
      <ResultEditor detail={detail} onRefresh={onRefresh} notify={notify} />
    </section>
  )
}

function ProblemPanel({ detail, onRefresh, onSettings, notify }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const act = async (path: string) => {
    try {
      await api(path, { method: 'POST' })
      await onRefresh()
      notify('任务已继续')
    } catch (error) { notify((error as Error).message, 'error') }
  }
  return (
    <div className="problem-panel">
      <AlertTriangle />
      <div><strong>{detail.task.status === 'NEEDS_MODEL' ? '模型连接需要处理' : '这一步需要你的选择'}</strong><p>{detail.task.last_error || '任务暂时没有完成。'}</p></div>
      <div className="problem-actions">
        {detail.task.status === 'NEEDS_MODEL' && <button onClick={onSettings}>去连接</button>}
        <button onClick={() => act(`/api/copy-tasks/${detail.task.id}/retry`)}><RefreshCw />重试</button>
        {normalizeStage(detail.task.stage) === 'RESEARCHER' && detail.task.constraints?.product_mode !== 'moments_life_case' && <button onClick={() => act(`/api/copy-tasks/${detail.task.id}/continue-without-search`)}>不联网继续</button>}
      </div>
    </div>
  )
}

function ResultEditor({ detail, onRefresh, notify }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const finalDraft = detail.drafts.find(item => item.is_final)
  const draft = finalDraft || detail.drafts[0]
  const review = detail.reviews[0]
  const productLabel = taskProductLabel(detail.task)
  const researchMode = String(detail.research?.packet.search_mode || '')
  const researchBadge = researchMode === 'unavailable' ? '联网受限，部分结论待核实'
    : researchMode === 'no_relevant_sources' ? '搜索结果与主题匹配不足'
    : researchMode === 'skipped' ? '本轮未联网'
      : researchMode === 'life_case_vision' ? '仅核对生活素材'
        : detail.task.web_research ? '已联网研究' : '未联网'
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [saving, setSaving] = useState(false)
  const [adoptingAngle, setAdoptingAngle] = useState('')
  const activeEmployee = employeeStages.find(item => item.key === normalizeStage(detail.task.stage))

  useEffect(() => {
    setTitle(draft?.package.title || '')
    setBody(draft?.package.body || draft?.body_text || '')
  }, [draft?.id])

  if (!draft) {
    if (!terminalStatuses.has(detail.task.status)) return (
      <div className="waiting-result">
        <span className="waiting-orb"><LoaderCircle className="spin" /></span>
        <div>
          <strong>{detail.task.status === 'QUEUED' ? '正在准备创作' : `${activeEmployee?.title || '数字员工'}正在工作`}</strong>
          <span>你可以先去查看其他作品，完成后定稿会自动显示在这里。</span>
        </div>
      </div>
    )
    return null
  }

  const copy = async () => {
    try {
      if (detail.task.constraints?.product_mode === 'moments_life_case') {
        await navigator.clipboard.writeText(body)
        notify('朋友圈文案已复制')
        return
      }
      const tags = (draft.package.tags || []).map(item => item.startsWith('#') ? item : `#${item}`).join(' ')
      await navigator.clipboard.writeText(`${title}\n\n${body}${tags ? `\n\n${tags}` : ''}`)
      notify(detail.task.content_type === 'xiaohongshu' ? '小红书发布版已复制' : '文案已复制')
    } catch { notify('复制失败，请手动选择正文', 'error') }
  }
  const save = async () => {
    setSaving(true)
    try {
      const mode = String(detail.task.constraints?.product_mode || '')
      const deliverables = draft.package.deliverables
        ? JSON.parse(JSON.stringify(draft.package.deliverables)) as ProductDeliverables
        : undefined
      if (deliverables && mode === 'xiaohongshu_ip') {
        deliverables.xiaohongshu_publish = { ...deliverables.xiaohongshu_publish, title, body, tags: draft.package.tags || [] }
      }
      if (deliverables && mode === 'douyin_emotion') {
        deliverables.douyin_script = { ...deliverables.douyin_script, script: body }
      }
      if (deliverables && mode === 'moments_ops') {
        deliverables.moments_posts = momentsPostsFromBody(body, deliverables.moments_posts)
      }
      await api(`/api/drafts/${draft.id}/edit`, jsonBody('POST', {
        title, body, cta: draft.package.cta || '', alternative_titles: draft.package.alternative_titles || [],
        platform_variants: draft.package.platform_variants || {}, tags: draft.package.tags || [],
        deliverables: deliverables || {}, claims: draft.package.claims || [], strategy: draft.package.strategy || {}, image_briefs: [],
        conversion_structure: draft.package.conversion_structure || {},
      }))
      await onRefresh()
      notify('修改已保存为新版本')
    } catch (error) { notify((error as Error).message, 'error') } finally { setSaving(false) }
  }

  const adoptVariant = async (variant: LifeCaseVariant) => {
    if (!draft.package.deliverables) return
    setAdoptingAngle(variant.angle)
    try {
      const deliverables = JSON.parse(JSON.stringify(draft.package.deliverables)) as ProductDeliverables
      deliverables.life_case_variants = (deliverables.life_case_variants || []).map(item => ({
        ...item,
        recommended: item.angle === variant.angle,
      }))
      await api(`/api/drafts/${draft.id}/edit`, jsonBody('POST', {
        title: draft.package.title || '生活案例朋友圈', body: variant.body, cta: '',
        alternative_titles: draft.package.alternative_titles || [], platform_variants: {},
        tags: [], deliverables, claims: draft.package.claims || [], strategy: draft.package.strategy || {}, image_briefs: [],
        conversion_structure: draft.package.conversion_structure || {},
      }))
      await onRefresh()
      notify(`已采用“${variant.label}”，并保存为新的最终稿`)
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setAdoptingAngle('')
    }
  }

  return (
    <div className="result-card">
      <div className="result-toolbar">
        <div><span className="eyebrow">{draft.is_final ? '主编定稿' : `当前版本 V${draft.version}`}</span><strong>{productLabel} · {researchBadge}</strong></div>
        <div>
          <button onClick={copy}><Copy />{detail.task.content_type === 'xiaohongshu' ? '复制发布版' : '复制'}</button>
          <button onClick={() => window.open(`/api/drafts/${draft.id}/export?format=md`, '_blank')}><Download />导出</button>
          <button onClick={save} disabled={saving}><Save />{saving ? '保存中' : '保存新版'}</button>
        </div>
      </div>
      <ProductDeliverablesPanel task={detail.task} deliverables={draft.package.deliverables} onAdoptVariant={adoptVariant} adoptingAngle={adoptingAngle} notify={notify} />
      {draft.package.deliverables && <div className="editor-label"><span>{detail.task.constraints?.product_mode === 'moments_life_case' ? '编辑当前采用稿' : '编辑总稿'}</span><small>修改后可保存为新版本，不覆盖原稿</small></div>}
      {detail.task.constraints?.product_mode !== 'moments_life_case' && <input className="title-editor" value={title} onChange={event => setTitle(event.target.value)} />}
      <textarea className={`body-editor ${draft.package.deliverables ? 'structured' : ''}`} value={body} onChange={event => setBody(event.target.value)} />
      <div className="delivery-summary">
        <span>{productLabel}</span>
        <p>{detail.task.constraints?.product_mode === 'moments_life_case' ? '不强加标题、标签或营销话术' : draft.package.cta ? `行动承接：${draft.package.cta}` : '主编已完成平台适配'}</p>
        {(draft.package.tags || []).length > 0 && <div>{draft.package.tags?.map(item => <i key={item}>#{item.replace(/^#/, '')}</i>)}</div>}
      </div>
      {draft.package.alternative_titles?.length > 0 && (
        <div className="title-options"><span>备选标题</span>{draft.package.alternative_titles.map(item => <button key={item} onClick={() => setTitle(item)}>{item}</button>)}</div>
      )}
      <div className="result-details">
        <StrategyDetails strategy={draft.package.strategy || (detail.research?.packet.strategy as DraftStrategy | undefined)} />
        <SourceDetails sources={detail.sources} research={detail.research?.packet} />
        <ReviewDetails review={review} />
      </div>
    </div>
  )
}

function ProductDeliverablesPanel({ task, deliverables, onAdoptVariant, adoptingAngle, notify }: {
  task: Task
  deliverables?: ProductDeliverables
  onAdoptVariant: (variant: LifeCaseVariant) => Promise<void>
  adoptingAngle: string
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const mode = String(task.constraints?.product_mode || '')
  if (!deliverables) return null

  const copyText = async (text: string, success: string) => {
    try {
      await navigator.clipboard.writeText(text)
      notify(success)
    } catch { notify('复制失败，请手动选择内容', 'error') }
  }

  if (mode === 'moments_life_case' && deliverables.life_case_variants?.length) {
    const labels: Record<string, string> = { daily: '真实日常', reflection: '有感而发', soft_business: '轻度业务启发' }
    const ordered = [...deliverables.life_case_variants].sort((a, b) => Number(b.recommended) - Number(a.recommended))
    return (
      <section className="product-deliverables life-variants-deliverable">
        <div className="deliverable-heading">
          <div><span className="eyebrow">主编已去 AI 味</span><h3>三种角度，都可以直接发</h3></div>
          <small>推荐版排在最前</small>
        </div>
        <div className="life-variant-grid">
          {ordered.map(variant => (
            <article className={`life-variant-card ${variant.recommended ? 'recommended' : ''}`} key={variant.angle}>
              <header>
                <div><strong>{variant.label || labels[variant.angle] || '朋友圈版本'}</strong>{variant.recommended && <span><Sparkles />主编推荐</span>}</div>
                {variant.rationale && <small>{variant.rationale}</small>}
              </header>
              <p>{variant.body}</p>
              <footer>
                <button onClick={() => copyText(variant.body, `“${variant.label || labels[variant.angle]}”已复制`)}><Copy />复制这版</button>
                <button className="adopt-button" disabled={variant.recommended || Boolean(adoptingAngle)} onClick={() => onAdoptVariant(variant)}>
                  {adoptingAngle === variant.angle ? <LoaderCircle className="spin" /> : <CheckCircle2 />}
                  {variant.recommended ? '当前采用' : '采用这版'}
                </button>
              </footer>
            </article>
          ))}
        </div>
      </section>
    )
  }

  if (mode === 'moments_ops' && deliverables.moments_posts?.length) {
    return (
      <section className="product-deliverables moments-deliverables">
        <div className="deliverable-heading"><div><span className="eyebrow">可直接发布</span><h3>朋友圈每日内容</h3></div><small>{deliverables.moments_posts.length} 天</small></div>
        <div className="moments-post-grid">
          {deliverables.moments_posts.map(post => (
            <article className="moments-post-card" key={`${post.day}-${post.time}`}>
              <header><strong>第 {post.day} 天</strong><span>{post.time || '时间自定'}</span></header>
              <div className="post-purpose"><i>{post.role || '日常内容'}</i><span>{post.purpose}</span></div>
              <p>{post.content}</p>
              {post.follow_up && <footer><strong>互动承接</strong><span>{post.follow_up}</span></footer>}
              <button onClick={() => copyText(post.content + (post.follow_up ? `\n\n互动承接：${post.follow_up}` : ''), `第 ${post.day} 天内容已复制`)}><Copy />复制这条</button>
            </article>
          ))}
        </div>
      </section>
    )
  }

  if (mode === 'xiaohongshu_ip' && deliverables.xiaohongshu_publish?.body) {
    const publish = deliverables.xiaohongshu_publish
    const tagLine = (publish.tags || []).map(item => `#${item.replace(/^#/, '')}`).join(' ')
    const fullText = `${publish.title}\n\n${publish.body}${tagLine ? `\n\n${tagLine}` : ''}`
    return (
      <section className="product-deliverables publish-deliverable">
        <div className="deliverable-heading"><div><span className="eyebrow">小红书发布版</span><h3>{publish.title}</h3></div><button onClick={() => copyText(fullText, '小红书发布版已复制')}><Copy />一键复制</button></div>
        <div className="publish-preview"><p>{publish.body}</p><div>{publish.tags.map(tag => <i key={tag}>#{tag.replace(/^#/, '')}</i>)}</div></div>
      </section>
    )
  }

  if (mode === 'douyin_emotion' && deliverables.douyin_script?.script) {
    const script = deliverables.douyin_script
    return (
      <section className="product-deliverables douyin-deliverable">
        <div className="deliverable-heading"><div><span className="eyebrow">抖音口播结构</span><h3>情绪推进已经拆好</h3></div><button onClick={() => copyText(script.script, '抖音口播稿已复制')}><Copy />复制口播稿</button></div>
        <div className="douyin-hook"><span>前 3 秒</span><strong>{script.hook}</strong></div>
        <div className="emotion-beats">{script.emotion_beats.map((beat, index) => <span key={`${index}-${beat}`}><i>{index + 1}</i>{beat}</span>)}</div>
        {script.ending && <div className="douyin-ending"><span>自然收束</span><p>{script.ending}</p></div>}
      </section>
    )
  }

  return null
}

function LifeCaseMaterial({ caseItem, compact = false }: { caseItem: LifeCase; compact?: boolean }) {
  const sortedMedia = [...caseItem.media].sort((a, b) => a.ordinal - b.ordinal)
  return (
    <section className={`life-case-material ${compact ? 'compact' : ''}`}>
      <header>
        <div><span className="eyebrow">本次生活素材</span><strong>{caseItem.title}</strong></div>
        <small>{caseItem.occurred_at ? caseItem.occurred_at.slice(0, 10) : '未填写日期'} · {sortedMedia.length} 张图片</small>
      </header>
      <div className="life-material-body">
        <div className="life-material-images">
          {sortedMedia.map(media => {
            const heic = /hei[cf]/i.test(media.mime_type) || /\.hei[cf]$/i.test(media.display_name)
            return (
              <figure key={media.id} title={media.display_name}>
                <div className="heic-placeholder"><Camera /><span>{heic ? 'HEIC' : '图片'}</span><small>{media.display_name}</small></div>
                {!heic && <img src={apiUrl(media.media_url)} alt={media.display_name} loading="lazy" onError={event => { event.currentTarget.style.display = 'none' }} />}
              </figure>
            )
          })}
        </div>
        <p>{caseItem.note_text}</p>
      </div>
      {!compact && <footer><ShieldCheck />原图保存在本机；生成时不会把图片地址写进文案或日志。</footer>}
    </section>
  )
}

function StrategyDetails({ strategy }: { strategy?: DraftStrategy }) {
  if (!strategy || (!strategy.controlling_idea && !strategy.argument_beats?.length && !strategy.surface_question)) return null
  return (
    <details className="strategy-details">
      <summary><PenLine />选题与逻辑拆解 <span>{strategy.argument_beats?.length || 0} 个推进节点</span><ChevronDown /></summary>
      <div className="details-body">
        {strategy.surface_question && <p><strong>表面问题</strong>{strategy.surface_question}</p>}
        {strategy.deeper_question && <p><strong>真正要回答</strong>{strategy.deeper_question}</p>}
        {strategy.persona_fit && <p><strong>人设匹配</strong>{strategy.persona_fit}</p>}
        {strategy.business_fit && <p><strong>业务连接</strong>{strategy.business_fit}</p>}
        {strategy.real_scene && <p><strong>切入口</strong>{strategy.real_scene}</p>}
        {strategy.controlling_idea && <p><strong>全文主判断</strong>{strategy.controlling_idea}</p>}
        {strategy.argument_beats?.map((beat, index) => (
          <article className="strategy-beat" key={`${index}-${beat.purpose || ''}`}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <div><strong>{beat.purpose || '论证节点'}</strong>{beat.reader_gain && <p>{beat.reader_gain}</p>}{beat.bridge && <small>承接：{beat.bridge}</small>}</div>
          </article>
        ))}
        {strategy.fact_boundaries?.length ? <div><strong>事实边界</strong>{strategy.fact_boundaries.map(item => <p key={item}>· {item}</p>)}</div> : null}
        {strategy.voice_direction && <p><strong>表达方向</strong>{strategy.voice_direction}</p>}
      </div>
    </details>
  )
}

function SourceDetails({ sources, research }: { sources: Source[]; research?: Record<string, unknown> }) {
  const observation = research?.life_case_observation as Record<string, unknown> | undefined
  const assessments = Array.isArray(research?.source_assessments)
    ? research.source_assessments as Array<{ source_id?: string; source_role?: string; relevance?: string; supported_points?: string[]; limitations?: string }>
    : []
  const assessmentById = new Map(assessments.map(item => [String(item.source_id || ''), item]))
  const searchMode = String(research?.search_mode || '')
  const searchWarning = searchMode === 'unavailable'
    ? '本轮公开搜索没有返回可核验网页。文案不会把品牌档案或资料库冒充为联网证据，涉及外部事实的内容应按待核实项处理。'
    : searchMode === 'no_relevant_sources' ? '公开搜索虽有返回，但与当前主题、人设或业务的匹配不足，已排除这些结果。文案中的外部事实按待核实项处理。'
    : searchMode === 'skipped' ? '本轮已按用户选择跳过联网研究。' : ''
  const observationGroups = observation ? [
    ['你提供的事实', observation.user_facts],
    ['图片中可确认', observation.visible_facts],
    ['主编不会擅自写入', observation.uncertain_items],
    ['隐私提醒', observation.privacy_notes],
  ].map(([label, value]) => ({ label: String(label), values: Array.isArray(value) ? value.map(String) : [] })).filter(group => group.values.length) : []
  return (
    <details>
      <summary>{observation ? <Camera /> : <Search />}{observation ? '素材识别' : '研究来源'} <span>{observation ? observationGroups.reduce((sum, group) => sum + group.values.length, 0) : sources.length}</span><ChevronDown /></summary>
      <div className="details-body">
        {searchWarning && <p className="search-warning">{searchWarning}</p>}
        {observationGroups.map(group => <div className="observation-group" key={group.label}><strong>{group.label}</strong>{group.values.map(item => <p key={item}>· {item}</p>)}</div>)}
        {research && <p className="research-brief">{String(research.brief || '')}</p>}
        {sources.length === 0 ? !observation && <p className="muted">本次没有使用外部来源。</p> : sources.map(source => (
          <article className="source-row" key={source.id}>
            <span>{source.source_key}</span>
            <div>
              {(() => {
                const assessment = assessmentById.get(source.source_key)
                return assessment ? <div className="source-assessment"><strong>{assessment.source_role || '来源'}{assessment.relevance ? ` · ${assessment.relevance}` : ''}</strong>{assessment.supported_points?.slice(0, 3).map((point, index) => <p key={`${index}-${point}`}>支持：{point}</p>)}{assessment.limitations && <small>局限：{assessment.limitations}</small>}</div> : null
              })()}
              {source.url ? <a href={source.url} target="_blank" rel="noreferrer">{source.title}<ExternalLink /></a> : <strong>{source.title}</strong>}
              <p>{source.excerpt || '来源已记录'}</p>
              <small>{source.metadata?.search_mode === 'openai_web_search' ? '模型联网来源' : ['rss_fallback', 'bing_rss', 'sogou_web', 'public_web'].includes(source.metadata?.search_mode || '') ? `公开联网来源${source.metadata?.search_focus ? ` · ${source.metadata.search_focus}` : ''}` : '资料库'} · {formatDate(source.metadata?.searched_at || source.published_at)}</small>
            </div>
          </article>
        ))}
      </div>
    </details>
  )
}

function ReviewDetails({ review }: { review?: Review }) {
  return (
    <details>
      <summary><ShieldCheck />主编意见 {review && <span>{review.total_score} 分</span>}<ChevronDown /></summary>
      <div className="details-body">
        {!review ? <p className="muted">主编尚未完成审核。</p> : <>
          <p className="review-summary">{review.change_summary}</p>
          {review.risks?.length > 0 && <div className="risk-list"><strong>需要留意</strong>{review.risks.map(item => <p key={item}>· {item}</p>)}</div>}
          {review.blocking_issues?.length > 0 && <div className="risk-list danger"><strong>仍需处理</strong>{review.blocking_issues.map(item => <p key={item}>· {item}</p>)}</div>}
        </>}
      </div>
    </details>
  )
}

function recommendationTitle(value: TopicRadarRecommendation | string | null | undefined) {
  if (typeof value === 'string') return value.trim()
  return String(value?.topic || value?.title || value?.angle || '').trim()
}

function recommendationReason(value: TopicRadarRecommendation | string | null | undefined) {
  if (!value || typeof value === 'string') return ''
  return String(value.reason || value.why || '').trim()
}

function platformSearchLinksForQuery(query: string): PlatformSearchLink[] {
  const clean = String(query || '').trim()
  if (!clean) return []
  return [
    {
      platform: 'douyin',
      label: '抖音搜索',
      url: `https://www.douyin.com/search/${encodeURIComponent(clean)}?type=video`,
      source_mode: 'official_search',
    },
    {
      platform: 'xiaohongshu',
      label: '小红书搜索',
      url: `https://www.xiaohongshu.com/search_result/?keyword=${encodeURIComponent(clean)}`,
      source_mode: 'official_search',
    },
  ]
}

function isOfficialPlatformSearchLink(value: unknown): value is PlatformSearchLink {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<PlatformSearchLink>
  if ((item.platform !== 'douyin' && item.platform !== 'xiaohongshu') || typeof item.url !== 'string') return false
  try {
    const parsed = new URL(item.url)
    if (parsed.protocol !== 'https:') return false
    const host = parsed.hostname.toLowerCase()
    return item.platform === 'douyin'
      ? host === 'douyin.com' || host.endsWith('.douyin.com')
      : host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')
  } catch { return false }
}

function resolvePlatformSearchLinks(rawLinks: unknown, query: string): PlatformSearchLink[] {
  const fallback = platformSearchLinksForQuery(query)
  const fromResponse = Array.isArray(rawLinks) ? rawLinks.filter(isOfficialPlatformSearchLink) : []
  const byPlatform = new Map(fromResponse.map(item => [item.platform, item]))
  return fallback.map(item => byPlatform.get(item.platform) || item)
}

function recommendationPlatformSearchLinks(value: TopicRadarRecommendation | string | null | undefined) {
  const query = typeof value === 'string'
    ? value
    : String(value?.search_query || recommendationTitle(value) || '').trim()
  return resolvePlatformSearchLinks(typeof value === 'string' ? undefined : value?.platform_search_links, query)
}

function recommendationSourceUrls(value: TopicRadarRecommendation | string | null | undefined, platformLinks: PlatformSearchLink[]) {
  const evidence = typeof value === 'string' || !value ? [] : Array.isArray(value.source_urls) ? value.source_urls : []
  return Array.from(new Set([...evidence.filter(url => /^https:\/\//i.test(url)), ...platformLinks.map(link => link.url)])).slice(0, 10)
}

function recommendationOriginalPost(value: TopicRadarRecommendation | string | null | undefined) {
  if (!value || typeof value === 'string') return null
  const candidates = [value.evidence_url, value.source_url, ...(value.source_urls || [])].filter((url): url is string => typeof url === 'string')
  for (const url of candidates) {
    try {
      const parsed = new URL(url)
      const host = parsed.hostname.toLowerCase()
      if ((host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')) && /\/(explore|discovery\/item|search_result)\//.test(parsed.pathname)) {
        return { url, label: '小红书原帖', platform: 'xiaohongshu' as const }
      }
      if ((host === 'douyin.com' || host.endsWith('.douyin.com')) && /\/(video|note)\//.test(parsed.pathname)) {
        return { url, label: '抖音原帖', platform: 'douyin' as const }
      }
    } catch { /* ignore malformed historic links */ }
  }
  return null
}

function xhsPostId(url: string) {
  try {
    return new URL(url).pathname.match(/\/(?:explore|discovery\/item|search_result)\/([A-Za-z0-9]+)/)?.[1] || ''
  } catch { return '' }
}

function xhsExtractionKey(url: string) {
  const postId = xhsPostId(url)
  return postId ? `post:${postId}` : url
}

function indexXhsExtractions(items: XiaohongshuExtraction[]) {
  return Object.fromEntries(items.map(item => [xhsExtractionKey(item.url), item]))
}

function findXhsExtraction(items: Record<string, XiaohongshuExtraction>, url: string) {
  return items[xhsExtractionKey(url)] || items[url]
}

function douyinVideoId(url: string) {
  try { return new URL(url).pathname.match(/\/video\/(\d+)/)?.[1] || '' } catch { return '' }
}

function douyinExtractionKey(url: string) {
  const videoId = douyinVideoId(url)
  return videoId ? `video:${videoId}` : url
}

function indexDouyinExtractions(items: DouyinExtraction[]) {
  return Object.fromEntries(items.map(item => [douyinExtractionKey(item.url), item]))
}

function findDouyinExtraction(items: Record<string, DouyinExtraction>, url: string) {
  return items[douyinExtractionKey(url)] || items[url]
}

function runRecommendations(run?: TopicRadarRun | null) {
  const values = (run?.summary?.recommendations || run?.summary?.recommended_topics || []) as unknown[]
  return values.filter((item): item is TopicRadarRecommendation | string => typeof item === 'string' || (Boolean(item) && typeof item === 'object'))
}

function radarJevBadge(value: { route?: string | null; classification_status?: string; jev?: Record<string, unknown>; raw?: Record<string, unknown> }) {
  const raw = value.raw || {}
  const jev = (value.jev && typeof value.jev === 'object' ? value.jev : raw.jev) as Record<string, unknown> | undefined
  const route = String(value.route || jev?.route || '')
  if (['A', 'B', 'C'].includes(route)) return `JEV ${route} 类`
  const status = String(value.classification_status || raw.classification_status || jev?.status || '')
  if (status === 'AWAITING_JEV_KEY') return '等待连接 JEV'
  if (status === 'ERROR') return 'JEV 待重试'
  return status ? 'JEV 分类中' : ''
}

type RadarPostStats = {
  xiaohongshu: number
  douyin: number
  liveVerified: number
  complete: number
  partial: number
  failed: number
  actionRequired: number
}

function radarPostStats(
  posts: Array<{ platform: string; url: string }>,
  xhs: Record<string, XiaohongshuExtraction>,
  douyin: Record<string, DouyinExtraction>,
  liveVerified: boolean,
): RadarPostStats {
  const result: RadarPostStats = {
    xiaohongshu: 0, douyin: 0, liveVerified: liveVerified ? posts.length : 0,
    complete: 0, partial: 0, failed: 0, actionRequired: 0,
  }
  const actionStatuses = new Set(['needs_verification', 'needs_login', 'needs_browser', 'USER_ACTION_REQUIRED', 'NEEDS_BROWSER', 'NEEDS_ATTENTION'])
  const pendingStatuses = new Set(['queued', 'extracting', 'ready_to_archive', 'QUEUED'])
  posts.forEach(post => {
    if (post.platform === 'xiaohongshu') {
      result.xiaohongshu += 1
      const extraction = findXhsExtraction(xhs, post.url)
      if (extraction?.archive?.archive_status === 'COMPLETE') result.complete += 1
      else if (extraction?.archive?.archive_status === 'PARTIAL' || extraction?.status === 'partial') result.partial += 1
      else if (actionStatuses.has(extraction?.status || '') || extraction?.archive_job?.status === 'PAUSED') result.actionRequired += 1
      else if (extraction && !pendingStatuses.has(extraction.status)) result.failed += 1
    } else if (post.platform === 'douyin') {
      result.douyin += 1
      const extraction = findDouyinExtraction(douyin, post.url)
      if (isCompleteDouyinPublishedCopy(extraction)) result.complete += 1
      else if (extraction && ['PARTIAL', 'COMPLETE'].includes(extraction.status)) result.partial += 1
      else if (actionStatuses.has(extraction?.status || '')) result.actionRequired += 1
      else if (extraction && !isPendingDouyinExtraction(extraction)) result.failed += 1
    }
  })
  return result
}

function RadarStats({ value }: { value: RadarPostStats }) {
  return <div className="radar-run-stats" aria-label="本轮内容统计">
    <span><b>{value.xiaohongshu}</b>小红书</span>
    <span><b>{value.douyin}</b>抖音</span>
    <span><b>{value.liveVerified}</b>实时验证</span>
    <span><b>{value.complete}</b>完整归档</span>
    <span><b>{value.partial}</b>部分归档</span>
    <span><b>{value.failed}</b>失败</span>
    <span><b>{value.actionRequired}</b>需操作</span>
  </div>
}

function XhsRichBody({ archive }: { archive: XiaohongshuArchive }) {
  const tokens = archive.body_rich || []
  const inlineAssets = new Map(
    archive.assets.filter(asset => asset.role === 'media_part').map(asset => [asset.ordinal, asset]),
  )
  if (!tokens.length) return <pre className="xhs-reading-copy">{archive.body_raw}</pre>
  return <div className="xhs-rich-copy" aria-label="正文原貌视图">
    {tokens.map((token, index) => {
      if (token.type === 'text') return <span key={index}>{token.text}</span>
      if (token.type === 'break') return <br key={index} />
      if (token.type === 'inline_image') {
        const asset = inlineAssets.get(Number(token.asset_ordinal))
        return asset?.download_url
          ? <img key={index} src={apiUrl(asset.download_url)} alt={token.alt || '原文内联表情'} className="xhs-inline-image" />
          : <span key={index} className="xhs-unresolved-inline">{token.alt || '[图片表情未保存]'}</span>
      }
      return <span key={index} className="xhs-unresolved-inline">{token.label || '[未解析的原文元素]'}</span>
    })}
  </div>
}

function XhsExtractionPanel({ extraction, onArchive, onRewrite, archiving = false }: {
  extraction?: XiaohongshuExtraction
  onArchive?: (resumeAfterUserAction?: boolean) => void
  onRewrite?: (extraction: XiaohongshuExtraction) => void
  archiving?: boolean
}) {
  const [copyFeedback, setCopyFeedback] = useState('')
  if (!extraction) return null
  const archive = extraction.archive
  const archiveJob = extraction.archive_job
  const successful = extraction.status === 'success' || extraction.status === 'partial' || Boolean(archive)
  const archivePending = archiving || archiveJob?.status === 'QUEUED' || archiveJob?.status === 'RUNNING'
  const pending = archiving || isPendingXhsExtraction(extraction)
  const archivePaused = archiveJob?.status === 'PAUSED'
  const browserRequired = extraction.status === 'needs_browser' || (archivePaused && archiveJob?.stage === 'NEEDS_BROWSER')
  const bodyVerified = archive?.body_extraction_status === 'VERIFIED'
  const pageStructureChanged = archive?.body_extraction_status === 'PAGE_STRUCTURE_CHANGED'
  const needsParserUpgrade = Boolean(archive && !['xhs-parser-v3', 'xhs-parser-v4'].includes(archive.parser_version || ''))
  const savedImages = archive?.assets.filter(item => item.role === 'carousel' && item.status === 'SAVED') || []
  const cover = archive?.assets.find(item => item.role === 'cover' && item.status === 'SAVED')
  const expectedImages = archiveJob?.progress.expected_asset_count ?? extraction.expected_asset_count
  const statusLabel = browserRequired ? '需要打开专用采集窗口后继续'
    : archivePaused ? '归档已暂停，请完成平台提示后继续'
    : bodyVerified
    ? archive?.archive_status === 'COMPLETE' ? '正文已验证，图文归档完整' : '正文已验证，图片或富文本归档部分完成'
    : pageStructureChanged ? '页面结构未识别，正文未核验'
    : archive ? '正文尚未验证，图文归档部分完成'
      : extraction.status === 'success' || extraction.status === 'partial' ? '旧版页面文本未验证，等待严格归档'
        : extraction.status === 'queued' ? '已排队，系统会自动提取'
          : extraction.status === 'extracting' || extraction.status === 'ready_to_archive' ? '正在按原帖 ID 校验正文并归档'
            : extraction.status === 'needs_verification' ? '需要完成小红书安全验证'
              : extraction.status === 'needs_login' ? '需要登录小红书'
                : extraction.status === 'platform_limited' ? '平台暂时限制访问'
                  : extraction.status === 'identity_mismatch' ? '帖子 ID 不一致，已拒绝覆盖'
                    : extraction.status === 'unavailable' ? '原帖已失效或不可访问' : '暂时无法提取'
  if (successful) return <details className="xhs-copy-disclosure">
    <summary><span><FileText />查看原文</span><b>{statusLabel} <ChevronDown /></b></summary>
    <section className="xhs-extraction-panel ready">
      <header><div><span className="platform-dot xhs">小</span><strong>{archivePending && <LoaderCircle className="spin" />}{archivePending ? archiveJob?.progress.message || '正在归档' : statusLabel}</strong></div>{archive && <b>{savedImages.length}/{expectedImages ?? '未知'} 张原图</b>}</header>
      <div className="xhs-extraction-meta"><span>{extraction.author || '作者未读取'}</span>{extraction.published_at && <span>{extraction.published_at}</span>}{extraction.tags?.length > 0 && <span>{extraction.tags.slice(0, 6).join(' ')}</span>}</div>
      {bodyVerified && archive?.body_raw != null
        ? <><small className="radar-summary-label">逐字原文 · 来源：{archive.body_source} · 原始换行、空格、符号与 Emoji 已保留</small><pre className="xhs-reading-copy" aria-label="逐字原文">{archive.body_raw}</pre><div className="extraction-copy-actions"><button className="xhs-extract-button" onClick={() => { void copyPlainText(archive.body_raw || '').then(() => setCopyFeedback('正文已复制')).catch(error => setCopyFeedback((error as Error).message)) }}><Copy />复制正文</button><button className="xhs-extract-button" onClick={() => downloadPlainText(extraction.title || '小红书正文', archive.body_raw || '')}><Download />导出正文 .txt</button></div>{copyFeedback && <small className="radar-summary-label" role="status">{copyFeedback}</small>}{Boolean(archive.body_rich?.length) && <details className="xhs-raw-proof"><summary>查看原帖页面排版参考</summary><XhsRichBody archive={archive} /></details>}</>
        : <div className="xhs-extraction-error"><AlertTriangle /><span>{archive?.body_extraction_message || '尚未获得与目标 post_id 一致且通过结构化来源校验的正文。搜索摘要和旧版 DOM/Meta 文本不会作为原帖全文展示。'}</span></div>}
      {(savedImages.length > 0 || cover) && <details className="xhs-image-disclosure">
        <summary><ImagePlus />查看图片（{savedImages.length + (cover ? 1 : 0)} 张已保存）<ChevronDown /></summary>
        <div className="xhs-archive-images">
          {cover?.download_url && <figure><img src={apiUrl(cover.download_url)} alt="小红书封面原始响应" /><figcaption>封面 · {cover.width || '?'}×{cover.height || '?'} · {cover.extension || ''}</figcaption></figure>}
          {savedImages.map(asset => asset.download_url && <figure key={asset.id}><img src={apiUrl(asset.download_url)} alt={`小红书原图 ${asset.ordinal}`} /><figcaption>第 {asset.ordinal} 张 · {asset.width || '?'}×{asset.height || '?'} · {asset.extension || ''}</figcaption></figure>)}
        </div>
      </details>}
      <div className="xhs-archive-proof"><span>最后尝试：{attemptTimeLabel(extraction.last_attempt_at, extraction.status)}</span><span>重试次数：{extraction.retry_count}</span><span>失败阶段：{extraction.failure_stage || '无'}</span>{archive && <><span>帖子 ID：{archive.post_id === archive.observed_post_id ? '已一致' : '不一致'}</span><span>正文提取：{archive.body_extraction_status || '待核验'}</span><span>图文归档：{archive.archive_status}</span><span>校验：{archive.manifest_sha256 ? '已生成清单哈希' : '待校验'}</span></>}</div>
      {onRewrite && bodyVerified && archive?.body_raw != null && archive.post_id === archive.observed_post_id && <button className="xhs-rewrite-button" onClick={() => onRewrite(extraction)}><WandSparkles />按这篇改写成内容包</button>}
      {onArchive && (archive?.archive_status !== 'COMPLETE' || needsParserUpgrade) && <button className="xhs-extract-button" onClick={() => onArchive(archivePaused)} disabled={archivePending}>{archivePending ? <LoaderCircle className="spin" /> : <Save />}{browserRequired ? '打开采集窗口后继续补齐' : archivePaused ? '完成登录或验证后继续补齐' : needsParserUpgrade ? '按新版正文解析重新归档' : archive ? '继续补齐缺失内容' : '保存原图与正文'}</button>}
      {(extraction.screenshot_url || extraction.body || extraction.image_text || extraction.video_subtitle || extraction.video_speech) && <details className="xhs-extraction-details"><summary>查看未核验的历史辅助证据 <ChevronDown /></summary>
        {extraction.screenshot_url && <details><summary>采集截图 <ChevronDown /></summary><img src={apiUrl(extraction.screenshot_url)} alt="小红书原帖采集截图" /></details>}
        {extraction.body && <details><summary>旧版页面可见文本（不作为原文） <ChevronDown /></summary><pre>{extraction.body}</pre></details>}
        {extraction.image_text && <details><summary>图片文字（OCR） <ChevronDown /></summary><pre>{extraction.image_text}</pre></details>}
        {extraction.video_subtitle && <details><summary>平台可见字幕 <ChevronDown /></summary><pre>{extraction.video_subtitle}</pre></details>}
        {extraction.video_speech && <details><summary>语音识别辅助文本 <ChevronDown /></summary><pre>{extraction.video_speech}</pre></details>}
      </details>}
      {archiveJob?.error_code && archive?.archive_status !== 'COMPLETE' && <div className="xhs-extraction-error"><AlertTriangle /><span>{archiveJob.progress.message || archiveJob.error_code}</span></div>}
      <footer><span>OCR 与字幕只作为辅助证据，不会补写原始正文。</span></footer>
    </section>
  </details>
  return <section className={`xhs-extraction-panel xhs-extraction-status ${pending ? 'pending' : 'failed'}`}>
    <header><div><span className="platform-dot xhs">小</span><strong>{pending && <LoaderCircle className="spin" />}{statusLabel}</strong></div></header>
    <div className="xhs-archive-proof"><span>最后尝试：{attemptTimeLabel(extraction.last_attempt_at, extraction.status)}</span><span>重试次数：{extraction.retry_count}</span><span>失败阶段：{extraction.failure_stage || '无'}</span></div>
    {extraction.error_message && <div className="xhs-extraction-error"><AlertTriangle /><span>{extraction.error_message}</span></div>}
    {onArchive && !pending && <button className="xhs-extract-button" onClick={() => onArchive(archivePaused)} disabled={archivePending}>{archivePending ? <LoaderCircle className="spin" /> : extraction.status === 'identity_mismatch' ? <RefreshCw /> : <Save />}{browserRequired ? '打开采集窗口后继续' : archivePaused ? '完成登录或验证后继续' : extraction.status === 'identity_mismatch' ? '重新核对该帖子 ID' : '保存原图与正文'}</button>}
  </section>
}

function asrSegmentTime(value: number | null): string {
  if (value == null || !Number.isFinite(value) || value < 0) return '时间未知'
  const minutes = Math.floor(value / 60)
  const seconds = (value % 60).toFixed(1).padStart(4, '0')
  return `${minutes}:${seconds}`
}

function DouyinExtractionPanel({ extraction: incomingExtraction, extracting, onExtract }: {
  extraction?: DouyinExtraction
  extracting: boolean
  onExtract: () => void
}) {
  const mediaInput = useRef<HTMLInputElement>(null)
  const [extraction, setExtraction] = useState(incomingExtraction)
  const [transcribing, setTranscribing] = useState(false)
  const [asrError, setAsrError] = useState('')
  const [copyFeedback, setCopyFeedback] = useState('')
  useEffect(() => setExtraction(incomingExtraction), [incomingExtraction])
  if (!extraction) return null
  const transcribeSelectedFile = async (file: File) => {
    const confirmed = window.confirm(
      `将只在本机转写你选中的文件“${file.name}”，不从抖音下载视频，也不上传到云端。\n\n请确认：该文件就是这条视频（ID ${extraction.video_id}），并且你有权处理其中的音视频内容。首次使用如本机模型未安装，系统可能下载 Apple 中文语音资源。`
    )
    if (!confirmed) return
    setAsrError('')
    setTranscribing(true)
    const form = new FormData()
    form.append('workspace_id', extraction.workspace_id)
    form.append('url', extraction.url)
    form.append('confirm_selected_media', 'true')
    form.append('file', file)
    try {
      const result = await api<{ extraction: DouyinExtraction }>('/api/topic-radar/douyin/transcribe-local', {
        method: 'POST', body: form, timeoutMs: 30 * 60 * 1000,
      })
      setExtraction(result.extraction)
    } catch (error) {
      setAsrError((error as Error).message)
    } finally { setTranscribing(false) }
  }
  const ready = extraction.status === 'PARTIAL' || extraction.status === 'COMPLETE'
  const queueTranscribing = extraction.status === 'TRANSCRIBING' || extraction.asr_status === 'PROCESSING'
  const pending = isPendingDouyinExtraction(extraction) || extracting
  const savedParts = [extraction.published_body ? '详情页文案片段' : '', extraction.search_result_text ? '搜索结果候选文本' : '', extraction.platform_subtitle ? '平台字幕' : '', extraction.asr_text ? '口播转写' : ''].filter(Boolean).join('与')
  const videoIdentityMatches = Boolean(extraction.video_id && extraction.video_id === extraction.observed_video_id)
  const videoIdentityLabel = !extraction.observed_video_id ? '未核验' : videoIdentityMatches ? '已一致' : '不一致'
  const hasPublishedBody = Boolean(videoIdentityMatches && extraction.published_body.trim())
  const fullPublishedCopy = isCompleteDouyinPublishedCopy(extraction)
  const retainedCaption = hasRetainedDouyinCaption(extraction)
  const transcriptQuality = douyinTranscriptQuality(extraction)
  const statusLabel = extraction.status === 'IDENTITY_MISMATCH' ? '视频 ID 不一致，已拒绝使用其他视频内容'
    : extraction.status === 'USER_ACTION_REQUIRED' ? '需要在采集浏览器中完成登录或安全验证'
      : extraction.status === 'POST_UNAVAILABLE' ? '视频已删除或当前账号不可访问'
        : extraction.status === 'NEEDS_BROWSER' ? '需要先打开本机采集浏览器'
          : queueTranscribing ? '详情已核验，正在本机临时转写口播'
          : ready ? retainedCaption ? '已保留此前完整文案，本次未重新核验' : fullPublishedCopy ? '已核验完整发布文案' : hasPublishedBody ? `已保存${savedParts || '可用字段'}（口播需单独转写）` : '详情未取得发布文案'
            : pending ? '正在后台读取视频详情' : '暂时无法读取视频详情'
  if (ready) return <details className="xhs-copy-disclosure douyin-copy-disclosure">
    <summary><span><FileText />查看详情采集结果</span><b>{statusLabel}{transcriptQuality.needsReview ? ` · ${transcriptQuality.label}` : extraction.asr_text && transcriptQuality.status === 'UNKNOWN' ? ' · 口播未评估' : ''} <ChevronDown /></b></summary>
    <section className="xhs-extraction-panel ready">
      <header><div><span className="platform-dot dy">抖</span><strong>{statusLabel}</strong></div><b>{extraction.video_status === 'TRANSIENT_MEDIA_PROCESSED' ? '已本机转写 · 媒体未留存' : extraction.video_status === 'MEDIA_UNAVAILABLE' ? '完整视频未归档' : extraction.video_status}</b></header>
      {(extraction.error_message || retainedCaption) && <div className="xhs-extraction-error"><AlertTriangle /><span>{extraction.error_message || '本次读取未重新核验全文，下方保留的是此前已核验的完整发布文案。'}</span></div>}
      <div className="xhs-extraction-meta"><span>{extraction.author || '作者未读取'}</span>{extraction.published_at && <span>{formatDate(extraction.published_at)}</span>}</div>
      {extraction.search_result_text && <><small className="radar-summary-label">搜索结果候选文本（不是详情页发布文案）{extraction.search_result_text_source ? ` · 来源：${extraction.search_result_text_source}` : ''}</small><pre className="xhs-reading-copy">{extraction.search_result_text}</pre></>}
      <small className="radar-summary-label">平台发布文案{fullPublishedCopy ? ' · 已从匹配视频详情读取' : ''}{extraction.published_body_source ? ` · 来源：${extraction.published_body_source}` : ''}</small>
      <pre className="xhs-reading-copy">{extraction.published_body || '页面未提供可读发布文案。'}</pre>
      {hasPublishedBody && <><div className="extraction-copy-actions"><button className="xhs-extract-button" onClick={() => { void copyPlainText(extraction.published_body).then(() => setCopyFeedback(fullPublishedCopy ? '发布文案已复制' : '已读取片段已复制（尚未核验为完整文案）')).catch(error => setCopyFeedback((error as Error).message)) }}><Copy />{fullPublishedCopy ? '复制发布文案' : '复制已读取片段'}</button><button className="xhs-extract-button" onClick={() => downloadPlainText(extraction.video_id ? `抖音_${extraction.video_id}` : fullPublishedCopy ? '抖音完整发布文案' : '抖音文案片段', extraction.published_body)}><Download />{fullPublishedCopy ? '导出完整发布文案 .txt' : '导出已读取片段 .txt'}</button></div>{copyFeedback && <small className="radar-summary-label" role="status">{copyFeedback}</small>}</>}
      {!fullPublishedCopy && <div className="xhs-extraction-error"><AlertTriangle /><span>{hasPublishedBody ? '当前保存的是详情页返回的文案片段，尚未核验完整。请重读详情补全文案；口播字幕不会替代发布文案。' : '尚未取得详情页发布文案。上方搜索结果文字只是候选线索，不是原帖正文；请在平台允许访问时重新读取详情。'}</span></div>}
      <small className="radar-summary-label">平台字幕{extraction.subtitle_source ? ` · 来源：${extraction.subtitle_source}` : ''}</small>
      <pre className="xhs-reading-copy">{extraction.platform_subtitle || '页面未提供可读平台字幕。'}</pre>
      {extraction.asr_text && <>
        <small className="radar-summary-label">口播语音识别{extraction.asr_engine ? ` · ${extraction.asr_engine}` : ''}{extraction.asr_model ? ` / ${extraction.asr_model}` : ''} · {extraction.asr_status === 'USER_MEDIA_TRANSCRIBED' ? '用户导入文件，本机转写' : extraction.asr_status === 'BROWSER_MEDIA_TRANSCRIBED' ? '登录态媒体，本机临时转写' : extraction.asr_status}</small>
        <div className={transcriptQuality.needsReview || transcriptQuality.status === 'UNKNOWN' ? 'xhs-extraction-error' : 'radar-summary-label'} role="status"><span><strong>{transcriptQuality.label}</strong>{transcriptQuality.reasons.length > 0 && `：${transcriptQuality.reasons.join('；')}`}</span></div>
        <pre className="xhs-reading-copy">{extraction.asr_text}</pre>
        {extraction.asr_confidence.assessed_segment_count != null && <small className="radar-summary-label">
          模型置信度均值 {Math.round((extraction.asr_confidence.mean || 0) * 100)}%（非准确率）· 已评估 {extraction.asr_confidence.assessed_segment_count} 段
          {extraction.asr_low_confidence.length > 0 ? ` · ${extraction.asr_low_confidence.length} 段建议重点核对` : ''}
        </small>}
        {extraction.asr_segments.length > 0 && <details className="douyin-asr-segments">
          <summary>查看时间分段与识别置信度（{extraction.asr_segments.length} 段）</summary>
          <ol>{extraction.asr_segments.map((segment, index) => <li key={`${index}-${segment.start_seconds ?? 'na'}`}>
            <small>{asrSegmentTime(segment.start_seconds)}–{asrSegmentTime(segment.end_seconds)}{segment.confidence == null ? '' : ` · ${Math.round(segment.confidence * 100)}%`}</small>
            <span>{segment.text}</span>
          </li>)}</ol>
        </details>}
        {extraction.asr_message && <small className="radar-summary-label">{extraction.asr_message}</small>}
        {extraction.asr_media_sha256 && <small className="radar-summary-label" title={extraction.asr_media_sha256}>转写来源文件 SHA-256：{extraction.asr_media_sha256.slice(0, 16)}…</small>}
      </>}
      {queueTranscribing && <small className="radar-summary-label">{extraction.asr_message || '正在读取当前登录会话可访问的媒体流，不会绕过登录、安全验证或平台限制。'}</small>}
      {!extraction.asr_text && extraction.asr_message && !queueTranscribing && <small className="radar-summary-label">{extraction.asr_message}</small>}
      <div className="xhs-archive-proof"><span>最后尝试：{attemptTimeLabel(extraction.last_attempt_at, extraction.status)}</span><span>重试次数：{extraction.retry_count}</span><span>失败阶段：{extraction.failure_stage || '无'}</span><span>视频 ID：{videoIdentityLabel}</span><span>本地 ASR：{extraction.asr_status}</span><span>画面 OCR：{extraction.ocr_status}</span></div>
      <input ref={mediaInput} type="file" accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.mov,.webm" hidden onChange={event => { const file = event.currentTarget.files?.[0]; if (file) void transcribeSelectedFile(file); event.currentTarget.value = '' }} />
      <button className="xhs-extract-button" onClick={() => mediaInput.current?.click()} disabled={transcribing}>{transcribing ? <LoaderCircle className="spin" /> : <FileText />}{transcribing ? '正在本机转写音频' : extraction.asr_text ? '更换本机文件并重新转写' : '导入本机视频 / 音频转写口播'}</button>
      {asrError && <div className="xhs-extraction-error"><AlertTriangle /><span>{asrError}</span></div>}
      <small className="radar-summary-label">若自动读取成功，媒体仅在本机临时目录用于转写并随即删除；用户导入文件也只在本机处理。平台文案、字幕与机器转写分开显示，ASR 结果仍需人工核对。</small>
      {(!fullPublishedCopy || retainedCaption) && !pending && <button className="xhs-extract-button" onClick={onExtract} disabled={extracting}>{extracting ? <LoaderCircle className="spin" /> : <RefreshCw />}{extracting ? '正在重读视频详情' : retainedCaption ? '重新读取并核验当前文案' : '重新读取详情，尝试补全文案'}</button>}
      <footer><span>登录态只用于读取当前浏览器可访问的作品；遇到登录、验证码或平台限制会暂停，不绕过安全机制。发布文案、字幕和口播转写是不同证据，不会互相冒充。</span></footer>
    </section>
  </details>
  return <section className={`xhs-extraction-panel xhs-extraction-status ${pending ? 'pending' : 'failed'}`}>
    <header><div><span className="platform-dot dy">抖</span><strong>{pending && <LoaderCircle className="spin" />}{statusLabel}</strong></div></header>
    {extraction.search_result_text && <><small className="radar-summary-label">搜索结果候选文本（不是详情页发布文案）{extraction.search_result_text_source ? ` · 来源：${extraction.search_result_text_source}` : ''}</small><pre className="xhs-reading-copy">{extraction.search_result_text}</pre></>}
    {extraction.error_message && <div className="xhs-extraction-error"><AlertTriangle /><span>{extraction.error_message}</span></div>}
    {!pending && <button className="xhs-extract-button" onClick={onExtract}>{extracting ? <LoaderCircle className="spin" /> : <RefreshCw />}{extraction.status === 'USER_ACTION_REQUIRED' ? '完成验证后重新读取' : extraction.status === 'IDENTITY_MISMATCH' ? '重新核对该视频 ID' : '重新读取详情'}</button>}
  </section>
}

const rewriteStageLabels: Array<{ key: RewriteStage; label: string }> = [
  { key: 'analysis', label: '拆解原帖' },
  { key: 'strategy', label: '确定策略' },
  { key: 'copy_generation', label: '生成内容包' },
  { key: 'image_plan_generation', label: '策划系列配图' },
  { key: 'review', label: '审核可发布性' },
]

function RewriteWorkbench({ workspaceId, extraction, onClose, notify }: {
  workspaceId: string
  extraction: XiaohongshuExtraction
  onClose: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const archive = extraction.archive
  const strictSourceReady = Boolean(
    archive?.body_raw !== undefined && archive?.body_raw !== null
    && archive.verification_status === 'VERIFIED'
    && archive.post_id === archive.observed_post_id,
  )
  const [targetPlatform, setTargetPlatform] = useState<'xiaohongshu' | 'douyin' | 'moments'>('xiaohongshu')
  const [strength, setStrength] = useState<'light' | 'balanced' | 'deep'>('balanced')
  const [persona, setPersona] = useState('自然、真诚、有具体生活观察的创作者')
  const [imageCount, setImageCount] = useState(6)
  const [additionalBrief, setAdditionalBrief] = useState('')
  const [job, setJob] = useState<RewriteJob | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [regenerating, setRegenerating] = useState<RewriteStage | null>(null)

  useEffect(() => {
    setJob(null)
    setSubmitting(false)
    setRegenerating(null)
  }, [extraction.id, archive?.id])

  useEffect(() => {
    if (!job || !['QUEUED', 'RUNNING'].includes(job.status)) return
    const timer = window.setInterval(async () => {
      try {
        const next = await api<RewriteJob>(`/api/rewrite/jobs/${job.id}?workspace_id=${encodeURIComponent(workspaceId)}`, { timeoutMs: 20000 })
        setJob(next)
        if (next.status === 'FAILED') notify(next.error_message || '改写任务没有完成', 'error')
        if (next.status === 'COMPLETED') notify('完整改写内容包已经生成')
      } catch (error) { notify((error as Error).message, 'error') }
    }, 1200)
    return () => window.clearInterval(timer)
  }, [job, notify, workspaceId])

  const start = async () => {
    if (!strictSourceReady || !archive) {
      notify('这篇帖子还没有通过“原始正文”校验，不能拿 OCR、摘要或字幕替代', 'error')
      return
    }
    setSubmitting(true)
    try {
      const next = await api<RewriteJob>('/api/rewrite/jobs', {
        ...jsonBody('POST', {
          workspace_id: workspaceId,
          extraction_id: extraction.id,
          archive_id: archive.id,
          target_platform: targetPlatform,
          rewrite_strength: strength,
          persona,
          image_count: imageCount,
          additional_brief: additionalBrief,
        }), timeoutMs: 20000,
      })
      setJob(next)
      notify('已开始按原帖做分阶段改写')
    } catch (error) { notify((error as Error).message, 'error') } finally { setSubmitting(false) }
  }

  const regenerate = async (stage: RewriteStage) => {
    if (!job) return
    setRegenerating(stage)
    try {
      const next = await api<RewriteJob>(`/api/rewrite/jobs/${job.id}/regenerate`, {
        ...jsonBody('POST', { workspace_id: workspaceId, stage }), timeoutMs: 20000,
      })
      setJob(next)
      notify(`正在重新生成「${rewriteStageLabels.find(item => item.key === stage)?.label || stage}」`)
    } catch (error) { notify((error as Error).message, 'error') } finally { setRegenerating(null) }
  }

  const exportPackage = () => {
    if (!job) return
    window.open(apiUrl(`/api/rewrite/jobs/${job.id}/export?workspace_id=${encodeURIComponent(workspaceId)}`), '_blank', 'noopener,noreferrer')
  }

  const copyPackage = async () => {
    const copy = job?.result.copy_generation
    if (!copy) return
    const text = [copy.title, '', copy.body, '', `标签：${copy.tags.map(tag => `#${tag.replace(/^#/, '')}`).join(' ')}`, '', `评论区引导：${copy.comment_cta}`].join('\n')
    try {
      await navigator.clipboard.writeText(text)
      notify('完整文案已复制')
    } catch { notify('浏览器没有允许复制，请手动选择文案', 'error') }
  }

  const result = job?.result
  const copy = result?.copy_generation
  const plan = result?.image_plan_generation
  const review = result?.review
  const sourceImages = archive?.assets.filter(asset => ['carousel', 'cover'].includes(asset.role) && asset.status === 'SAVED') || []

  return <section className="rewrite-workbench" aria-label="原帖改写工作台">
    <header className="rewrite-workbench-heading">
      <div><span className="eyebrow">原帖 → 新内容包</span><h2>按这篇改写</h2><p>原文与原图只读保存；新文案独立生成，不会覆盖归档。</p></div>
      <button className="icon-button" onClick={onClose} aria-label="关闭改写工作台"><X /></button>
    </header>
    <div className="rewrite-workbench-grid">
      <aside className="rewrite-source-panel">
        <div className="rewrite-source-heading"><span className="platform-dot xhs">小</span><div><b>{extraction.title || '未命名原帖'}</b><small>{extraction.author || '作者未读取'} · {archive?.archive_status === 'COMPLETE' ? '正文与原图完整归档' : '正文已校验，原图待补齐'}</small></div></div>
        {!strictSourceReady && <div className="rewrite-source-warning"><AlertTriangle /><span>需要先完成已验证的原始正文归档，才能进入改写。</span></div>}
        {archive?.body_raw != null && <><small className="radar-summary-label">只读逐字原文 · 原始格式保留</small><pre className="rewrite-source-body">{archive.body_raw}</pre></>}
        {sourceImages.length > 0 && <div className="rewrite-source-images">{sourceImages.map(asset => asset.download_url && <img key={asset.id} src={apiUrl(asset.download_url)} alt={`原帖第 ${asset.ordinal} 张图片`} />)}</div>}
        <div className="rewrite-source-proof"><span>帖子 ID：{archive?.post_id === archive?.observed_post_id ? '已核验' : '未核验'}</span><span>原图：{sourceImages.length}/{extraction.expected_asset_count ?? '未知'} 张</span></div>
      </aside>
      <div className="rewrite-output-panel">
        {!job && <div className="rewrite-options">
          <div className="rewrite-option-row"><label>目标平台<select value={targetPlatform} onChange={event => setTargetPlatform(event.target.value as typeof targetPlatform)}><option value="xiaohongshu">小红书图文</option><option value="douyin">抖音口播</option><option value="moments">朋友圈</option></select></label><label>改写强度<select value={strength} onChange={event => setStrength(event.target.value as typeof strength)}><option value="light">轻度：保留核心主题</option><option value="balanced">均衡：换角度与结构</option><option value="deep">深度：重做叙事与表达</option></select></label><label>配图数量<select value={imageCount} onChange={event => setImageCount(Number(event.target.value))}>{[3, 4, 5, 6, 7, 8, 9].map(value => <option key={value} value={value}>{value} 张</option>)}</select></label></div>
          <label>人设与表达方向<textarea value={persona} onChange={event => setPersona(event.target.value)} maxLength={300} /></label>
          <label>这次补充要求（可选）<textarea value={additionalBrief} onChange={event => setAdditionalBrief(event.target.value)} maxLength={1200} placeholder="例如：更适合刚开始做内容的女生；不要提具体产品。" /></label>
          <button className="primary-button" onClick={start} disabled={!strictSourceReady || submitting}>{submitting ? <LoaderCircle className="spin" /> : <WandSparkles />}{submitting ? '正在建立改写任务' : '开始生成完整内容包'}</button>
        </div>}
        {job && <>
          <div className="rewrite-stage-strip">{rewriteStageLabels.map(item => {
            const step = [...job.steps].reverse().find(value => value.stage === item.key)
            const completed = Boolean(result?.[item.key])
            const running = job.status === 'RUNNING' && job.stage === item.key
            return <div key={item.key} className={`${completed ? 'done' : ''} ${running ? 'running' : ''}`}><span>{completed ? <CheckCircle2 /> : running ? <LoaderCircle className="spin" /> : '·'}</span><b>{item.label}</b>{completed && <button onClick={() => regenerate(item.key)} disabled={Boolean(regenerating) || ['QUEUED', 'RUNNING'].includes(job.status)}>{regenerating === item.key ? <LoaderCircle className="spin" /> : <RefreshCw />}重做</button>}{step?.status === 'FAILED' && <small>{step.error_message || '本环节失败'}</small>}</div>
          })}</div>
          {['QUEUED', 'RUNNING'].includes(job.status) && <div className="rewrite-running"><LoaderCircle className="spin" />正在{rewriteStageLabels.find(item => item.key === job.stage)?.label || '准备内容包'}，完成后会自动显示。</div>}
          {job.status === 'FAILED' && <div className="rewrite-source-warning"><AlertTriangle /><span>{job.error_message || '任务没有完成'}。可以重做失败环节。</span></div>}
          {copy && <section className="rewrite-copy-package"><header><div><span className="eyebrow">可发布文案</span><h3>{copy.title}</h3></div><div><button onClick={copyPackage}><Copy />复制文案</button><button onClick={exportPackage}><Download />导出</button></div></header><div className="rewrite-title-list">{copy.alternative_titles.map(title => <span key={title}>{title}</span>)}</div><div className="rewrite-cover-copy">封面字：{copy.cover_copy}</div><pre>{copy.body}</pre><footer><span>{copy.tags.map(tag => `#${tag.replace(/^#/, '')}`).join(' ')}</span><b>评论区：{copy.comment_cta}</b></footer></section>}
          {plan && <section className="rewrite-image-plan"><header><div><span className="eyebrow">系列配图策划</span><h3>{plan.style_bible.visual_tone}</h3><p>{plan.style_bible.palette.join(' · ')} · {plan.style_bible.lighting}</p></div><button onClick={() => regenerate('image_plan_generation')} disabled={Boolean(regenerating) || ['QUEUED', 'RUNNING'].includes(job.status)}><RefreshCw />重做配图</button></header><div className="rewrite-image-cards">{plan.image_cards.map(card => <article key={card.id}><span>{String(card.ordinal).padStart(2, '0')}</span><div><b>{card.role} · {card.caption}</b><small>对应 {card.related_copy_segment}</small><p>{card.visual_notes}</p><details><summary>查看生成提示词</summary><pre>{card.prompt}</pre></details></div></article>)}</div></section>}
          {review && <section className={`rewrite-review ${review.decision === 'PASS' ? 'pass' : 'revise'}`}><ShieldCheck /><div><b>{review.decision === 'PASS' ? '审核通过，可继续制作' : '建议先调整再发布'}</b><p>{review.final_notes}</p>{review.issues.length > 0 && <small>{review.issues.join('；')}</small>}</div></section>}
        </>}
      </div>
    </div>
  </section>
}

function xhsNeedsRetry(extraction?: XiaohongshuExtraction) {
  return Boolean(extraction && !['success', 'partial', 'queued', 'extracting', 'ready_to_archive', 'identity_mismatch'].includes(extraction.status))
}

function xhsRetryLabel(extraction?: XiaohongshuExtraction) {
  if (extraction?.status === 'needs_browser') return '打开专用采集窗口后重试'
  if (extraction?.status === 'needs_verification') return '完成安全验证后重试'
  if (extraction?.status === 'needs_login') return '登录小红书后重试'
  if (extraction?.status === 'platform_limited') return '稍后重新提取'
  if (extraction?.status === 'unavailable') return '重新搜索可访问原帖'
  return '按原帖 ID 重新校验并归档'
}

function DailyVerificationGate({ verification, browserStatus, opening, busy, onOpenBrowser, onRetry }: {
  verification: DailyVerificationState
  browserStatus: PlatformBrowserStatus | null
  opening: boolean
  busy: boolean
  onOpenBrowser: () => void
  onRetry: () => void
}) {
  const verified = verification.phase === 'verified'
  const running = verification.phase === 'running'
  const browserAvailable = browserStatus?.available !== false
  const collector = collectorBrowserState(browserStatus)
  return <section className={`radar-verification-gate ${verified ? 'verified' : running ? 'running' : 'needs-action'}`} aria-live="polite">
    <span className="radar-verification-icon">
      {verified ? <ShieldCheck /> : running ? <LoaderCircle className="spin" /> : <WifiOff />}
    </span>
    <div className="radar-verification-copy">
      <strong>{verified ? '本轮实时验证已完成' : running ? '正在完成本轮实时验证' : '本轮实时验证尚未完成'}</strong>
      <p>{verification.message}</p>
      {!verified && !running && <small>完成登录或安全验证后重新刷新；在验证成功前，历史保存内容不会进入今天的 IP 方向或原帖列表。</small>}
      {verification.hiddenHistoricalCount > 0 && <small>已从本轮展示中隔离 {verification.hiddenHistoricalCount} 条非实时来源内容。</small>}
    </div>
    {!verified && !running && <div className="radar-verification-actions">
      <button className="secondary-button" onClick={onOpenBrowser} disabled={opening || !browserAvailable}>{opening ? <LoaderCircle className="spin" /> : <ExternalLink />}{opening ? '正在打开' : collector.openLabel}</button>
      <button className="primary-button" onClick={onRetry} disabled={busy || !browserAvailable}>{busy ? <LoaderCircle className="spin" /> : <RefreshCw />}{collector.ready ? '重新验证并找热点' : '打开采集窗口'}</button>
    </div>}
  </section>
}

function RadarPage({ workspaceId, onCreateFromTopic, onTaskCreated, notify }: {
  workspaceId: string
  onCreateFromTopic: (seed: RadarSeed) => void
  onTaskCreated: (value: TaskDetail) => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [monitors, setMonitors] = useState<TopicMonitor[]>([])
  const [dailyRuns, setDailyRuns] = useState<TopicRadarRun[]>([])
  const [browserStatus, setBrowserStatus] = useState<PlatformBrowserStatus | null>(null)
  const [openingBrowser, setOpeningBrowser] = useState(false)
  const [dailyAction, setDailyAction] = useState<'enable' | 'run' | ''>('')
  const [topicQuery, setTopicQuery] = useState('')
  const [topicResult, setTopicResult] = useState<TopicSearchResponse | null>(null)
  const [selectedAngle, setSelectedAngle] = useState('')
  const [searchMode, setSearchMode] = useState<ProductMode>('xiaohongshu_ip')
  const [searchingTopic, setSearchingTopic] = useState(false)
  const [creatingFromTopic, setCreatingFromTopic] = useState(false)
  const [xhsExtractions, setXhsExtractions] = useState<Record<string, XiaohongshuExtraction>>({})
  const [douyinExtractions, setDouyinExtractions] = useState<Record<string, DouyinExtraction>>({})
  const [extractingUrl, setExtractingUrl] = useState('')
  const [extractingDouyinUrl, setExtractingDouyinUrl] = useState('')
  const [retryingMissingDouyin, setRetryingMissingDouyin] = useState(false)
  const [archivingId, setArchivingId] = useState('')
  const [selectedXhs, setSelectedXhs] = useState<Record<string, { url: string; title: string; search_keyword: string }>>({})
  const [archiveBatch, setArchiveBatch] = useState<XiaohongshuArchiveBatch | null>(null)
  const [batchSubmitting, setBatchSubmitting] = useState(false)
  const [rewriteSource, setRewriteSource] = useState<XiaohongshuExtraction | null>(null)
  const [favorites, setFavorites] = useState<RadarFavorite[]>([])
  const [radarPersonas, setRadarPersonas] = useState<RadarPersona[]>([])
  const [batchPersonaId, setBatchPersonaId] = useState('')
  const [batchTarget, setBatchTarget] = useState(300)
  const [researchBatch, setResearchBatch] = useState<RadarSearchBatch | null>(null)
  const [batchCandidates, setBatchCandidates] = useState<RadarBatchCandidate[]>([])
  const [batchCandidateTotal, setBatchCandidateTotal] = useState(0)
  const [batchRoute, setBatchRoute] = useState<'all' | 'A' | 'B' | 'C'>('all')
  const [startingResearchBatch, setStartingResearchBatch] = useState(false)
  const [resumingResearchBatch, setResumingResearchBatch] = useState(false)
  const [resumingBatchDetails, setResumingBatchDetails] = useState(false)
  const dailyMonitor = useMemo(() => monitors.find(item => item.monitor_kind === 'ip_daily') || null, [monitors])

  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(`xhs-archive-selection:${workspaceId}`) || '{}')
      setSelectedXhs(saved && typeof saved === 'object' ? saved : {})
    } catch { setSelectedXhs({}) }
  }, [workspaceId])

  useEffect(() => {
    localStorage.setItem(`xhs-archive-selection:${workspaceId}`, JSON.stringify(selectedXhs))
  }, [selectedXhs, workspaceId])

  const toggleXhsSelection = (url: string, title: string, searchKeyword: string) => {
    setSelectedXhs(current => {
      const next = { ...current }
      if (next[url]) delete next[url]
      else next[url] = { url, title, search_keyword: searchKeyword }
      return next
    })
  }

  const loadXhsExtractions = useCallback(async () => {
    const saved = await api<XiaohongshuExtraction[]>(`/api/topic-radar/xhs/extractions?workspace_id=${encodeURIComponent(workspaceId)}&limit=500`)
    setXhsExtractions(indexXhsExtractions(saved))
    return saved
  }, [workspaceId])

  const loadDouyinExtractions = useCallback(async () => {
    const saved = await api<DouyinExtraction[]>(`/api/topic-radar/douyin/extractions?workspace_id=${encodeURIComponent(workspaceId)}`)
    setDouyinExtractions(indexDouyinExtractions(saved))
    return saved
  }, [workspaceId])

  const retryMissingDouyinTranscripts = async () => {
    setRetryingMissingDouyin(true)
    try {
      const result = await api<{ queued: number }>(`/api/topic-radar/douyin/extractions/retry-missing-transcripts?workspace_id=${encodeURIComponent(workspaceId)}`, { method: 'POST' })
      await loadDouyinExtractions()
      notify(result.queued ? `已将 ${result.queued} 条缺失或需核对的抖音口播加入本机转写队列` : '本次没有新增待补齐或重做的抖音口播任务')
    } catch (error) { notify((error as Error).message, 'error') } finally { setRetryingMissingDouyin(false) }
  }

  const submitArchiveBatch = async () => {
    const items = Object.values(selectedXhs)
    if (!items.length) return
    setBatchSubmitting(true)
    try {
      const batch = await api<XiaohongshuArchiveBatch>('/api/topic-radar/xhs/archive-batches', {
        ...jsonBody('POST', { workspace_id: workspaceId, items }), timeoutMs: 20000,
      })
      setArchiveBatch(batch)
      setSelectedXhs({})
      await loadXhsExtractions()
      notify(`已把 ${items.length} 篇帖子加入串行归档队列`)
    } catch (error) { notify((error as Error).message, 'error') } finally { setBatchSubmitting(false) }
  }

  const refreshArchiveBatch = useCallback(async () => {
    if (!archiveBatch?.id) return
    const next = await api<XiaohongshuArchiveBatch>(`/api/topic-radar/xhs/archive-batches/${archiveBatch.id}?workspace_id=${encodeURIComponent(workspaceId)}`)
    setArchiveBatch(next)
    return next
  }, [archiveBatch?.id, workspaceId])
  const archiveBatchPending = isPendingXhsArchiveBatch(archiveBatch)
  useEffect(() => {
    if (!archiveBatchPending) return
    const timer = window.setInterval(async () => {
      try {
        await refreshArchiveBatch()
        await loadXhsExtractions()
      } catch { /* keep the last visible batch state */ }
    }, 2000)
    return () => window.clearInterval(timer)
  }, [archiveBatchPending, loadXhsExtractions, refreshArchiveBatch])

  const loadDailyRuns = useCallback(async (monitorId: string) => {
    if (!monitorId) {
      setDailyRuns([])
      return [] as TopicRadarRun[]
    }
    const value = await api<TopicRadarRun[]>(`/api/topic-radar/monitors/${monitorId}/runs?workspace_id=${encodeURIComponent(workspaceId)}&limit=5`)
    setDailyRuns(value)
    return value
  }, [workspaceId])

  const loadMonitors = useCallback(async () => {
    const value = await api<TopicMonitor[]>(`/api/topic-radar/monitors?workspace_id=${encodeURIComponent(workspaceId)}`)
    setMonitors(value)
    return value
  }, [workspaceId])

  const loadFavorites = useCallback(async () => {
    const value = await api<RadarFavorite[]>(`/api/topic-radar/favorites?workspace_id=${encodeURIComponent(workspaceId)}`)
    setFavorites(value)
    return value
  }, [workspaceId])

  const loadBatchCandidates = useCallback(async (
    batchId: string,
    route: 'all' | 'A' | 'B' | 'C' = batchRoute,
    offset = 0,
  ) => {
    const routeQuery = route === 'all' ? '' : `&route=${route}`
    const page = await api<RadarBatchCandidatePage>(`/api/radar-batches/${batchId}/candidates?workspace_id=${encodeURIComponent(workspaceId)}${routeQuery}&limit=60&offset=${offset}`)
    setBatchCandidates(current => offset > 0 ? [...current, ...page.items] : page.items)
    setBatchCandidateTotal(page.total)
    return page
  }, [batchRoute, workspaceId])

  const loadResearchBatch = useCallback(async (batchId: string) => {
    const value = await api<RadarSearchBatch>(`/api/radar-batches/${batchId}?workspace_id=${encodeURIComponent(workspaceId)}`)
    setResearchBatch(value)
    await loadBatchCandidates(batchId, batchRoute, 0)
    return value
  }, [batchRoute, loadBatchCandidates, workspaceId])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [items, collectorStatus, savedExtractions, savedDouyinExtractions, savedFavorites, personas, batches] = await Promise.all([
          api<TopicMonitor[]>(`/api/topic-radar/monitors?workspace_id=${encodeURIComponent(workspaceId)}`),
          api<PlatformBrowserStatus>('/api/topic-radar/browser/status'),
          api<XiaohongshuExtraction[]>(`/api/topic-radar/xhs/extractions?workspace_id=${encodeURIComponent(workspaceId)}&limit=500`),
          api<DouyinExtraction[]>(`/api/topic-radar/douyin/extractions?workspace_id=${encodeURIComponent(workspaceId)}`),
          api<RadarFavorite[]>(`/api/topic-radar/favorites?workspace_id=${encodeURIComponent(workspaceId)}`),
          api<RadarPersona[]>(`/api/radar-batches/personas?workspace_id=${encodeURIComponent(workspaceId)}`),
          api<RadarSearchBatch[]>(`/api/radar-batches?workspace_id=${encodeURIComponent(workspaceId)}&limit=5`),
        ])
        const daily = items.find(item => item.monitor_kind === 'ip_daily')
        const dailyHistory = daily ? await api<TopicRadarRun[]>(`/api/topic-radar/monitors/${daily.id}/runs?workspace_id=${encodeURIComponent(workspaceId)}&limit=5`) : []
        if (cancelled) return
        setMonitors(items)
        setBrowserStatus(collectorStatus)
        setDailyRuns(dailyHistory)
        setXhsExtractions(indexXhsExtractions(savedExtractions))
        setDouyinExtractions(indexDouyinExtractions(savedDouyinExtractions))
        setFavorites(savedFavorites)
        setRadarPersonas(personas)
        setBatchPersonaId(current => current || personas[0]?.id || '')
        setResearchBatch(batches[0] || null)
        if (batches[0]) await loadBatchCandidates(batches[0].id, batchRoute, 0)
      } catch (error) {
        if (!cancelled) notify((error as Error).message, 'error')
      }
    }
    load()
    return () => { cancelled = true }
  }, [batchRoute, loadBatchCandidates, notify, workspaceId])

  const hasAutomaticExtraction = Object.values(xhsExtractions).some(isPendingXhsExtraction)
    || Object.values(douyinExtractions).some(isPendingDouyinExtraction)
  useEffect(() => {
    if (!hasAutomaticExtraction) return
    const timer = window.setInterval(() => {
      Promise.all([loadXhsExtractions(), loadDouyinExtractions()]).catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [hasAutomaticExtraction, loadXhsExtractions, loadDouyinExtractions])

  const serverDailyRefreshing = dailyRuns.some(item => item.status === 'RUNNING')
  useEffect(() => {
    if (!dailyMonitor || !serverDailyRefreshing) return
    const timer = window.setInterval(async () => {
      try {
        const rows = await loadDailyRuns(dailyMonitor.id)
        if (!rows.some(item => item.status === 'RUNNING')) {
          await Promise.all([loadMonitors(), loadXhsExtractions(), loadDouyinExtractions()])
        }
      } catch { /* the next poll can recover a temporary local request failure */ }
    }, 1500)
    return () => window.clearInterval(timer)
  }, [dailyMonitor, serverDailyRefreshing, loadDailyRuns, loadMonitors, loadXhsExtractions, loadDouyinExtractions])

  const researchBatchRunning = Boolean(researchBatch && ['QUEUED', 'COLLECTING', 'CLASSIFYING'].includes(researchBatch.status))
  useEffect(() => {
    if (!researchBatch?.id || !researchBatchRunning) return
    const timer = window.setInterval(async () => {
      try {
        const next = await loadResearchBatch(researchBatch.id)
        if (!['QUEUED', 'COLLECTING', 'CLASSIFYING'].includes(next.status)) {
          await Promise.all([loadXhsExtractions(), loadDouyinExtractions()])
        }
      } catch { /* keep the last durable progress and retry on the next poll */ }
    }, 1600)
    return () => window.clearInterval(timer)
  }, [loadDouyinExtractions, loadResearchBatch, loadXhsExtractions, researchBatch?.id, researchBatchRunning])

  useEffect(() => {
    if (!researchBatch?.id) return
    loadBatchCandidates(researchBatch.id, batchRoute, 0).catch(() => undefined)
  }, [batchRoute, loadBatchCandidates, researchBatch?.id])

  const openPlatformBrowser = async (queryOverride = '') => {
    setOpeningBrowser(true)
    try {
      const value = await api<PlatformBrowserStatus>('/api/topic-radar/browser/open', jsonBody('POST', {
        platform: 'both', query: queryOverride.trim() || topicQuery.trim() || dailyMonitor?.query || '相亲 脱单',
      }))
      setBrowserStatus(value)
      notify(collectorBrowserState(value).ready
        ? '双平台采集浏览器已打开；请完成平台页面上的登录或验证后，再继续读取。'
        : value.message || '采集窗口尚未就绪，请检查专用浏览器。', collectorBrowserState(value).ready ? 'ok' : 'error')
    } catch (error) { notify((error as Error).message, 'error') } finally { setOpeningBrowser(false) }
  }

  const enableDaily = async () => {
    setDailyAction('enable')
    try {
      const result = await api<{ monitor?: TopicMonitor; latest_run?: TopicRadarRun; latestRun?: TopicRadarRun; run?: TopicRadarRun }>('/api/topic-radar/ip-daily/enable', {
        ...jsonBody('POST', { workspace_id: workspaceId }), timeoutMs: 15000,
      })
      const nextMonitors = await loadMonitors()
      const monitor = result.monitor || nextMonitors.find(item => item.monitor_kind === 'ip_daily')
      const latestRun = result.latest_run || result.latestRun || result.run
      if (latestRun) setDailyRuns(current => [latestRun, ...current.filter(item => item.id !== latestRun.id)])
      else if (monitor) await loadDailyRuns(monitor.id)
      await Promise.all([loadXhsExtractions(), loadDouyinExtractions()])
      notify('每日 IP 热点已开启，会按你保存的人设自动更新')
    } catch (error) { notify((error as Error).message, 'error') } finally { setDailyAction('') }
  }

  const runDaily = async () => {
    setDailyAction('run')
    try {
      const result = await api<{ monitor?: TopicMonitor; latest_run?: TopicRadarRun; latestRun?: TopicRadarRun; run?: TopicRadarRun }>('/api/topic-radar/ip-daily/run', jsonBody('POST', { workspace_id: workspaceId }))
      const latestRun = result.latest_run || result.latestRun || result.run
      const monitor = result.monitor || dailyMonitor
      if (latestRun) setDailyRuns(current => [latestRun, ...current.filter(item => item.id !== latestRun.id)])
      else if (monitor) await loadDailyRuns(monitor.id)
      if (latestRun?.status === 'RUNNING') {
        notify('热点已开始更新，完成后会自动显示')
      } else {
        await Promise.all([loadMonitors(), loadXhsExtractions(), loadDouyinExtractions()])
        const directions = runRecommendations(latestRun).length
        notify(directions ? `今天已整理 ${directions} 个抖音和小红书方向` : '今日 IP 方向已更新')
      }
    } catch (error) { notify((error as Error).message, 'error') } finally { setDailyAction('') }
  }

  const retryDailyVerification = async () => {
    if (!collectorBrowserState(browserStatus).ready) {
      await openPlatformBrowser(dailyMonitor?.query || '')
      return
    }
    await runDaily()
  }

  const toggleMonitor = async (monitor: TopicMonitor) => {
    try {
      const next = await api<TopicMonitor>(`/api/topic-radar/monitors/${monitor.id}?workspace_id=${encodeURIComponent(workspaceId)}`, jsonBody('PATCH', { enabled: !monitor.enabled }))
      setMonitors(current => current.map(item => item.id === next.id ? next : item))
      notify(next.enabled ? '雷达已恢复定时刷新' : '雷达已暂停')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const searchTopic = async () => {
    const topic = topicQuery.trim()
    if (!topic) {
      notify('先输入这次想搜的主题', 'error')
      return null
    }
    if (!batchPersonaId) {
      notify('先选择这一批内容对应的人设账号', 'error')
      return null
    }
    setSearchingTopic(true)
    setStartingResearchBatch(true)
    try {
      const result = await api<RadarSearchBatch>('/api/radar-batches', {
        ...jsonBody('POST', {
          workspace_id: workspaceId,
          topic,
          persona_id: batchPersonaId,
          platforms: ['xiaohongshu', 'douyin'],
          target_count: batchTarget,
          origin: 'topic_search',
          skip_classification: true,
        }), timeoutMs: 20000,
      })
      setResearchBatch(result)
      setBatchCandidates([])
      setBatchCandidateTotal(0)
      setBatchRoute('all')
      setTopicResult(null)
      setSelectedAngle('')
      setBrowserStatus(await api<PlatformBrowserStatus>('/api/topic-radar/browser/status'))
      notify(`已启动 ${batchTarget} 条双平台候选采集；本批跳过 JEV，正文归档会独立排队。`)
      return result
    } catch (error) {
      notify((error as Error).message, 'error')
      return null
    } finally {
      setSearchingTopic(false)
      setStartingResearchBatch(false)
    }
  }

  const resumeResearchBatch = async () => {
    if (!researchBatch) return
    if (!collectorBrowserState(browserStatus).ready) {
      await openPlatformBrowser(researchBatch.topic)
      return
    }
    setResumingResearchBatch(true)
    try {
      const value = await api<RadarSearchBatch>(`/api/radar-batches/${researchBatch.id}/resume?workspace_id=${encodeURIComponent(workspaceId)}&skip_classification=true&resume_details=true`, { method: 'POST' })
      setResearchBatch(value)
      setBatchRoute('all')
      notify('已从上次进度继续；本批不会调用 JEV，并会重试本人验证后可继续读取的正文任务。')
    } catch (error) { notify((error as Error).message, 'error') } finally { setResumingResearchBatch(false) }
  }

  const cancelResearchBatch = async () => {
    if (!researchBatch) return
    try {
      const value = await api<RadarSearchBatch>(`/api/radar-batches/${researchBatch.id}/cancel?workspace_id=${encodeURIComponent(workspaceId)}`, { method: 'POST' })
      setResearchBatch(value)
      notify('已请求停止本批；当前正在完成的单条平台读取结束后会停下。')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const resumeBatchDetails = async () => {
    if (!researchBatch || !canResumeRadarDetails(researchBatch)) return
    if (!collectorBrowserState(browserStatus).ready) {
      await openPlatformBrowser(researchBatch.topic)
      return
    }
    setResumingBatchDetails(true)
    try {
      const result = await api<{ batch_id: string; queued: number }>(`/api/radar-batches/${researchBatch.id}/resume-details?workspace_id=${encodeURIComponent(workspaceId)}`, { method: 'POST' })
      await Promise.all([loadXhsExtractions(), loadDouyinExtractions(), refreshArchiveBatch()])
      notify(result.queued > 0
        ? `已将本批 ${result.queued} 条未完成正文加入读取队列，结果会自动更新。`
        : '本批未新增正文任务；当前提取状态已刷新。')
    } catch (error) { notify((error as Error).message, 'error') } finally { setResumingBatchDetails(false) }
  }

  const extractXhsPost = async (url: string, title: string, searchKeyword: string, quiet = false, force = false) => {
    const existing = findXhsExtraction(xhsExtractions, url)
    if (existing?.archive_job?.status === 'PAUSED') {
      await archiveXhsPost(existing, true)
      return existing
    }
    if (!collectorBrowserState(browserStatus).ready) {
      await openPlatformBrowser(searchKeyword)
      return null
    }
    setExtractingUrl(url)
    try {
      const result = await api<{ extraction: XiaohongshuExtraction }>('/api/topic-radar/xhs/extract', jsonBody('POST', {
        workspace_id: workspaceId,
        url,
        title_hint: title,
        search_keyword: searchKeyword,
        force,
      }))
      setXhsExtractions(current => ({ ...current, [xhsExtractionKey(result.extraction.url || url)]: result.extraction }))
      await refreshArchiveBatch()
      const jobStatus = result.extraction.archive_job?.status
      if (!quiet && result.extraction.archive?.body_extraction_status === 'VERIFIED') {
        notify('同一 post_id 的正文已验证；图片或富文本完整性单独显示。')
      } else if (!quiet && jobStatus === 'PAUSED') {
        notify('归档已暂停；请先在采集浏览器完成平台要求的操作，再点“完成登录或验证后继续”。', 'error')
      } else if (!quiet && ['QUEUED', 'RUNNING'].includes(jobStatus || '')) {
        notify('已加入严格归档队列；只有同一原帖的正文校验通过后才会显示为原文。')
      } else if (!quiet) {
        notify(result.extraction.error_message || '未获取到经过原帖身份校验的正文', 'error')
      }
      return result.extraction
    } catch (error) {
      if (!quiet) notify((error as Error).message, 'error')
      return null
    } finally {
      setExtractingUrl('')
    }
  }

  const extractDouyinPost = async (url: string, title: string, searchKeyword: string, quiet = false) => {
    setExtractingDouyinUrl(url)
    try {
      const result = await api<{ extraction: DouyinExtraction; queued?: boolean }>('/api/topic-radar/douyin/extract?background=true', jsonBody('POST', {
        workspace_id: workspaceId, url, title_hint: title, search_keyword: searchKeyword,
      }))
      setDouyinExtractions(current => ({ ...current, [douyinExtractionKey(result.extraction.url || url)]: result.extraction }))
      if (isPendingDouyinExtraction(result.extraction)) {
        if (!quiet) notify('已加入抖音详情读取队列，完成后会自动显示发布文案。')
        return result.extraction
      }
      if (result.extraction.status === 'PARTIAL' || result.extraction.status === 'COMPLETE') {
        try {
          await api<{ resumed: number }>(`/api/topic-radar/douyin/extractions/resume?workspace_id=${encodeURIComponent(workspaceId)}`, jsonBody('POST', {}))
          // Discover the re-queued rows so the normal extraction poll starts.
          await loadDouyinExtractions()
        } catch (error) {
          if (!quiet) notify(`本条读取结果已保存，其余抖音队列恢复失败：${(error as Error).message}`, 'error')
          return result.extraction
        }
      }
      if (!quiet) notify(result.extraction.status === 'PARTIAL' || result.extraction.status === 'COMPLETE'
        ? isCompleteDouyinPublishedCopy(result.extraction) ? '已核验并读取该视频的完整发布文案；口播字幕仍分开显示。' : '已读取页面可见发布文案和平台字幕；缺失部分不会用标题或摘要补齐。'
        : result.extraction.error_message || '暂时无法读取这条抖音视频详情', result.extraction.status === 'PARTIAL' || result.extraction.status === 'COMPLETE' ? 'ok' : 'error')
      return result.extraction
    } catch (error) {
      if (!quiet) notify((error as Error).message, 'error')
      return null
    } finally { setExtractingDouyinUrl('') }
  }

  const archiveXhsPost = async (extraction: XiaohongshuExtraction, resumeAfterUserAction = false) => {
    if (!collectorBrowserState(browserStatus).ready) {
      await openPlatformBrowser(extraction.search_keyword)
      return
    }
    setArchivingId(extraction.id)
    try {
      const response = await api<{ extraction: XiaohongshuExtraction; job: { status: string; progress?: { message?: string } } }>(
        '/api/topic-radar/xhs/archive',
        { ...jsonBody('POST', {
          workspace_id: workspaceId,
          extraction_id: extraction.id,
          resume_after_user_action: resumeAfterUserAction,
        }), timeoutMs: 15000 },
      )
      if (response.extraction) {
        setXhsExtractions(current => ({
          ...current,
          [xhsExtractionKey(response.extraction.url || extraction.url)]: response.extraction,
        }))
      }
      await refreshArchiveBatch()
      notify(response.job.progress?.message || (response.job.status === 'PAUSED' ? '请先在采集浏览器中完成登录或安全验证' : '已开始保存原图与正文'))
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setArchivingId('')
    }
  }

  const archiveXhsUrl = async (url: string, title: string, searchKeyword: string) => {
    setArchivingId(url)
    try {
      const response = await api<{ extraction: XiaohongshuExtraction; job: { status: string; progress?: { message?: string } } }>(
        '/api/topic-radar/xhs/archive',
        { ...jsonBody('POST', {
          workspace_id: workspaceId,
          url,
          title_hint: title,
          search_keyword: searchKeyword,
        }), timeoutMs: 15000 },
      )
      if (response.extraction) {
        setXhsExtractions(current => ({
          ...current,
          [xhsExtractionKey(response.extraction.url || url)]: response.extraction,
        }))
      }
      await refreshArchiveBatch()
      notify(response.job.progress?.message || '已开始保存原图与正文')
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setArchivingId('')
    }
  }

  const createFromTopicSearch = async () => {
    const query = topicQuery.trim()
    if (!query) {
      notify('先输入这次想研究的主题', 'error')
      return
    }
    if (!researchBatch || researchBatch.topic !== query) {
      const started = await searchTopic()
      if (started) notify('批量研究已经启动；JEV 分类完成后再选择 A 类内容进入创作。')
      return
    }
    if (['QUEUED', 'COLLECTING', 'CLASSIFYING'].includes(researchBatch.status)) {
      notify('这批内容还在采集或分类，完成后才能进入创作。', 'error')
      return
    }
    if (researchBatch.status === 'AWAITING_JEV_KEY') {
      notify('候选池已经采集完成，请先到设置连接 JEV，再继续分类。', 'error')
      return
    }
    if (researchBatch.status === 'USER_ACTION_REQUIRED') {
      notify('平台需要本人完成登录或安全验证；处理后点击“继续本批”。', 'error')
      return
    }
    const bothLive = researchBatch.platforms.every(platform => researchBatch.platform_status[platform]?.verification_status === 'LIVE_VERIFIED')
    if (!bothLive) {
      notify('只有本批所选平台都达到 LIVE_VERIFIED，才能把候选内容带入创作。', 'error')
      return
    }
    let eligible: RadarBatchCandidate[] = []
    try {
      const page = await api<RadarBatchCandidatePage>(`/api/radar-batches/${researchBatch.id}/candidates?workspace_id=${encodeURIComponent(workspaceId)}&route=A&limit=10&offset=0`)
      eligible = page.items
    } catch (error) {
      notify((error as Error).message, 'error')
      return
    }
    if (!eligible.length) {
      notify('本批没有通过 JEV 与规则闸门的 A 类内容；可以查看 B 类人工复核项或换一个主题。', 'error')
      return
    }
    const selected = eligible.find(item => item.title === selectedAngle) || eligible[0]
    const topic = selected.title || query
    const urls = [selected, ...eligible.filter(item => item.id !== selected.id)].slice(0, 10).map(item => item.url)
    setCreatingFromTopic(true)
    try {
      const response = await api<TaskDetail | { detail?: TaskDetail }>('/api/topic-radar/topic-search/create', jsonBody('POST', {
        workspace_id: workspaceId,
        topic,
        source_urls: urls,
        product_mode: searchMode,
        constraints: {
          radar_origin: 'jev_batch',
          radar_batch_id: researchBatch.id,
          radar_persona_id: researchBatch.persona_id,
          radar_search_query: query,
          jev_rubric_version: researchBatch.taxonomy_version,
        },
      }))
      const detail = 'task' in response ? response : response.detail
      if (!detail?.task) throw new Error('创作任务已提交，但没有收到任务详情')
      await onTaskCreated(detail)
      notify('已把本批 A 类真实内容带入创作流程')
    } catch (error) { notify((error as Error).message, 'error') } finally { setCreatingFromTopic(false) }
  }

  const favoriteForItem = (item?: TopicRadarItem) => item
    ? favorites.find(value => value.platform === item.platform && value.source_key === item.source_key)
    : undefined

  const favoriteForTopicSource = (source: Source) => {
    if (source.metadata?.platform !== 'xiaohongshu' || !source.url) return undefined
    const sourceKey = xhsExtractionKey(source.url)
    return favorites.find(value => value.platform === 'xiaohongshu' && xhsExtractionKey(value.url) === sourceKey)
  }

  const removeFavorite = async (favorite: RadarFavorite) => {
    try {
      await api<void>(`/api/topic-radar/favorites/${favorite.id}?workspace_id=${encodeURIComponent(workspaceId)}`, { method: 'DELETE' })
      setFavorites(current => current.filter(value => value.id !== favorite.id))
      notify('已取消收藏')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const toggleFavorite = async (item: TopicRadarItem) => {
    const existing = favoriteForItem(item)
    if (existing) {
      await removeFavorite(existing)
      return
    }
    try {
      const saved = await api<RadarFavorite>('/api/topic-radar/favorites', jsonBody('POST', {
        workspace_id: workspaceId,
        source_item_id: item.id,
      }))
      setFavorites(current => [saved, ...current.filter(value => value.id !== saved.id)])
      notify('已收藏这篇原帖')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const toggleTopicSourceFavorite = async (source: Source) => {
    const existing = favoriteForTopicSource(source)
    if (existing) {
      await removeFavorite(existing)
      return
    }
    if (source.metadata?.platform !== 'xiaohongshu' || !source.url) return
    try {
      const saved = await api<RadarFavorite>('/api/topic-radar/favorites/source-snapshot', jsonBody('POST', {
        workspace_id: workspaceId,
        source: {
          platform: 'xiaohongshu',
          url: source.url,
          title: source.title || '',
          author: source.metadata?.author || '',
          excerpt: source.excerpt || '',
          metrics: source.metadata?.metrics || {},
        },
      }))
      setFavorites(current => [saved, ...current.filter(value => value.id !== saved.id)])
      notify('已收藏这篇原帖')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const activeDailyRun = dailyRuns.find(item => item.status === 'RUNNING')
  // A refresh is a verification boundary.  Do not display a previous finished
  // run while the newest one is still reading the platforms: it looks like the
  // old cards passed today's verification when they did not.
  const dailyRun = activeDailyRun || dailyRuns.find(item => item.status !== 'RUNNING')
  const dailyVerification = dailyVerificationState(dailyRun, Boolean(activeDailyRun))
  const dailyRecommendations = dailyVerification.liveRecommendations
  const dailyVerifiedItems = dailyVerification.liveItems
  const dailyPlatforms = dailyRun?.summary?.platforms || []
  const dailyJev = dailyRun?.summary?.jev_classification
  const dailyJevPending = dailyJev?.status === 'AWAITING_JEV_KEY'
  const dailyVisibleSignals = dailyVerifiedItems.length
  const dailyPreviouslySeen = Number(dailyRun?.summary?.previously_seen_count || 0)
  const dailyFreshnessLabel = dailyVerification.phase === 'running'
    ? dailyVerification.message
    : dailyRun?.completed_at
      ? dailyVerification.phase === 'verified' && dailyVisibleSignals > 0
        ? `更新于 ${formatDate(dailyRun.completed_at)} · 本轮实时读取 ${dailyVisibleSignals} 条平台帖子`
        : dailyVerification.phase === 'verified' && dailyPreviouslySeen > 0
            ? `更新于 ${formatDate(dailyRun.completed_at)} · 本轮没有新的匹配帖子，已排除 ${dailyPreviouslySeen} 条历史内容`
            : `更新于 ${formatDate(dailyRun.completed_at)} · ${dailyVerification.message}`
      : '正在整理双平台方向'
  const topicAngles = topicResult?.angles || topicResult?.recommendations || []
  const topicSources = topicResult?.sources || topicResult?.items || []
  const dailyStats = radarPostStats(
    dailyVerifiedItems.map(item => ({ platform: item.platform, url: item.url })),
    xhsExtractions, douyinExtractions, dailyVerification.phase === 'verified',
  )
  const topicStats = radarPostStats(
    topicSources.map(source => ({ platform: source.metadata?.platform || 'web', url: source.url })),
    xhsExtractions, douyinExtractions, Boolean(topicResult?.live_verified),
  )
  const savedXhsExtractions = Object.values(xhsExtractions).sort((a, b) => String(b.extracted_at).localeCompare(String(a.extracted_at)))
  const savedXhsWithExactBody = savedXhsExtractions.filter(item =>
    item.archive?.body_extraction_status === 'VERIFIED' && item.archive.post_id === item.archive.observed_post_id,
  ).length
  const savedXhsPending = savedXhsExtractions.filter(item =>
    ['queued', 'extracting', 'ready_to_archive'].includes(item.status)
    || ['QUEUED', 'RUNNING'].includes(item.archive_job?.status || ''),
  ).length
  const savedDouyinExtractions = Object.values(douyinExtractions).sort((a, b) => String(b.extracted_at).localeCompare(String(a.extracted_at)))
  const savedDouyinWithFullCopy = savedDouyinExtractions.filter(isCompleteDouyinPublishedCopy).length
  const savedDouyinTranscriptReview = savedDouyinExtractions.filter(item => douyinTranscriptQuality(item).needsReview).length
  const savedDouyinTranscriptUnknown = savedDouyinExtractions.filter(item => Boolean(item.asr_text) && douyinTranscriptQuality(item).status === 'UNKNOWN').length
  const selectedBatchPersona = radarPersonas.find(item => item.id === batchPersonaId) || radarPersonas[0]
  const collector = collectorBrowserState(browserStatus)
  const missingDouyinTranscriptCount = Object.values(douyinExtractions).filter(canRetryDouyinTranscript).length
  const batchProgress = researchBatch?.counts.target
    ? Math.min(100, Math.round((researchBatch.counts.collected / researchBatch.counts.target) * 100))
    : 0
  const batchCanResume = Boolean(researchBatch && ['AWAITING_JEV_KEY', 'USER_ACTION_REQUIRED', 'INTERRUPTED', 'PARTIAL', 'FAILED'].includes(researchBatch.status))
  const batchStatusLabel: Record<string, string> = {
    QUEUED: '等待开始', COLLECTING: '正在批量采集', CLASSIFYING: 'JEV 正在分类', COMPLETED: '候选采集已完成',
    PARTIAL: '部分完成', AWAITING_JEV_KEY: '等待连接 JEV', USER_ACTION_REQUIRED: '需要本人处理平台验证',
    INTERRUPTED: '已中断，可继续', FAILED: '执行失败，可继续', CANCELED: '已停止',
  }

  return (
    <div className="radar-page">
      <section className="radar-hero">
        <div>
          <span className="eyebrow"><span className="radar-pulse" />GATHER PLANET · 内容雷达</span>
          <h1>热点，不用等你<em>先想到关键词。</em></h1>
          <p>每天按你已保存的人设生成抖音和小红书的搜索方向；有具体主题时，也会先准备两个平台的真实内容入口。</p>
        </div>
        <div className="radar-hero-orbit" aria-hidden="true"><Search /><i /><b /><span>HOT</span></div>
      </section>

      <section className={`radar-browser-card ${collector.ready ? 'connected' : ''}`}>
        <div className="radar-browser-copy">
          <span className="radar-browser-icon"><ShieldCheck /></span>
          <div><strong>平台采集浏览器</strong><p>{browserStatus?.message || '正在检查本机浏览器状态…'}</p><small>首次扫码后，Cookie 和登录会话会一直保存在这台电脑的专用 Chrome 资料中；平台主动让会话过期或触发风控时，仍需本人重新验证。</small></div>
        </div>
        <div className="radar-browser-actions">
          <span className={`radar-status-chip ${collector.ready ? 'on' : ''}`}><span />{collector.label}</span>
          <button className="primary-button" onClick={() => openPlatformBrowser()} disabled={openingBrowser || browserStatus?.available === false}>{openingBrowser ? <LoaderCircle className="spin" /> : <ExternalLink />}{openingBrowser ? '正在打开' : collector.openLabel}</button>
          {missingDouyinTranscriptCount > 0 && <button className="secondary-button" onClick={retryMissingDouyinTranscripts} disabled={retryingMissingDouyin || !collector.ready}>{retryingMissingDouyin ? <LoaderCircle className="spin" /> : <FileText />}{retryingMissingDouyin ? '正在排队' : `补齐或重做口播 ${missingDouyinTranscriptCount} 条`}</button>}
        </div>
      </section>

      <section className="radar-research-batch">
        <header className="radar-research-batch-heading">
          <div><span className="eyebrow">BATCH RESEARCH · ORIGINAL COPY</span><h2>先搜几百条，再逐条抓取原文</h2><p>本批只绑定一个人设。工作台按平台真实 ID 保存候选并排队读取正文；本轮跳过 JEV 分类，搜索完成不代表每条原文都已归档。</p></div>
          <span className={`radar-status-chip ${researchBatchRunning ? 'on' : ''}`}><span />{researchBatch ? batchStatusLabel[researchBatch.status] || researchBatch.status : '尚未开始'}</span>
        </header>
        <div className="radar-batch-config">
          <label><span>本批人设</span><select value={batchPersonaId} onChange={event => setBatchPersonaId(event.target.value)} disabled={researchBatchRunning}>{radarPersonas.map(persona => <option key={persona.id} value={persona.id}>{persona.label}</option>)}</select></label>
          <label><span>候选目标</span><select value={batchTarget} onChange={event => setBatchTarget(Number(event.target.value))} disabled={researchBatchRunning}><option value={200}>200 条</option><option value={300}>300 条</option><option value={500}>500 条</option></select></label>
          <div className="radar-batch-persona-copy"><strong>{selectedBatchPersona?.label || '读取人设中'}</strong><small>{selectedBatchPersona?.business || '这一批会按已保存的人设与业务边界分类。'}</small></div>
        </div>
        {researchBatch ? <div className="radar-batch-dashboard">
          <div className="radar-batch-progress-copy"><div><strong>“{researchBatch.topic}”</strong><small>{researchBatch.persona?.label} · 目标 {researchBatch.counts.target} 条 · 已采集 {researchBatch.counts.collected} 条 · {researchBatch.skip_classification ? '本批跳过 JEV 分类' : `已分类 ${researchBatch.counts.classified} 条`}</small></div><b>候选进度 {batchProgress}%</b></div>
          <div className="radar-batch-progress"><span style={{ width: `${batchProgress}%` }} /></div>
          <div className="radar-batch-stats"><span><b>{researchBatch.counts.collected}</b>候选池</span>{researchBatch.skip_classification ? <span><b>独立处理</b>正文/媒体归档</span> : <><span className="route-a"><b>{researchBatch.counts.A}</b>A · 优先推荐</span><span className="route-b"><b>{researchBatch.counts.B}</b>B · 人工复核</span><span className="route-c"><b>{researchBatch.counts.C}</b>C · 暂不推荐</span></>}</div>
          <div className="radar-daily-platforms">{researchBatch.platforms.map(platform => {
            const status = researchBatch.platform_status[platform]
            const ready = status?.verification_status === 'LIVE_VERIFIED'
            return <div className={ready ? 'ready' : 'needs-verification'} key={platform}><span className={`platform-dot ${platform === 'douyin' ? 'dy' : 'xhs'}`}>{platform === 'douyin' ? '抖' : '小'}</span><div><strong>{platform === 'douyin' ? '抖音' : '小红书'} {status?.collected || 0}/{status?.target || 0}</strong><small>{status?.message || '等待本轮平台读取'}</small></div></div>
          })}</div>
          {researchBatch.error_message && <div className="radar-notice"><AlertTriangle /><span>{researchBatch.error_message}</span></div>}
          <div className="radar-batch-actions">
            {batchCanResume && <button className="primary-button" onClick={resumeResearchBatch} disabled={resumingResearchBatch || resumingBatchDetails || openingBrowser}>{resumingResearchBatch ? <LoaderCircle className="spin" /> : <RefreshCw />}{resumingResearchBatch ? '正在继续' : '继续本批'}</button>}
            {canResumeRadarDetails(researchBatch) && <button className="secondary-button" onClick={resumeBatchDetails} disabled={resumingBatchDetails || resumingResearchBatch || openingBrowser}>{resumingBatchDetails ? <LoaderCircle className="spin" /> : <FileText />}{resumingBatchDetails ? '正在恢复正文队列' : '继续未完成正文'}</button>}
            {researchBatchRunning && <button className="secondary-button" onClick={cancelResearchBatch}>停止本批</button>}
            {researchBatch.status === 'AWAITING_JEV_KEY' && <small>该旧批次已保存候选；点击“继续本批”会跳过 JEV 分类并接着补充平台候选。</small>}
            {researchBatch.status === 'USER_ACTION_REQUIRED' && <button className="secondary-button" onClick={() => openPlatformBrowser(researchBatch.topic)} disabled={openingBrowser}>{openingBrowser ? <LoaderCircle className="spin" /> : <ExternalLink />}打开验证页</button>}
          </div>
          {researchBatch.counts.collected > 0 && <div className="radar-batch-results">
            {!researchBatch.skip_classification && <div className="radar-batch-route-tabs">{(['all', 'A', 'B', 'C'] as const).map(route => <button key={route} className={batchRoute === route ? 'selected' : ''} onClick={() => setBatchRoute(route)}>{route === 'all' ? `全部 ${researchBatch.counts.collected}` : `${route} 类 ${researchBatch.counts[route]}`}</button>)}</div>}
            <div className="radar-batch-candidate-grid">{batchCandidates.map(candidate => {
              const xhsExtraction = candidate.platform === 'xiaohongshu' ? findXhsExtraction(xhsExtractions, candidate.url) : undefined
              const douyinExtraction = candidate.platform === 'douyin' ? findDouyinExtraction(douyinExtractions, candidate.url) : undefined
              const actionRequired = researchBatch.status === 'USER_ACTION_REQUIRED'
              const candidateLiveVerified = candidate.verification_status === 'LIVE_VERIFIED'
              const candidatePlatformNeedsAction = researchBatch.platform_status[candidate.platform]?.verification_status === 'USER_ACTION_REQUIRED'
              const candidateDetailStatus = researchBatchRunning
                ? candidateLiveVerified ? '候选已核验；本轮搜索停稳后独立进入详情读取' : '等待候选完成本轮实时核验'
                : candidatePlatformNeedsAction
                  ? '本平台要求本人完成验证；此候选详情会在验证后继续读取'
                  : candidateLiveVerified
                    ? '候选已实时核验；详情读取独立推进，不等待另一平台补足数量'
                    : '候选尚未实时核验，不作为原文'
              const canReadCandidateDetails = !researchBatchRunning && candidateLiveVerified && !candidatePlatformNeedsAction
              return <article key={candidate.id} className={`radar-batch-candidate route-${(candidate.route || 'pending').toLowerCase()} ${selectedAngle === candidate.title ? 'selected' : ''}`}>
                <header><span className={`platform-dot ${candidate.platform === 'douyin' ? 'dy' : 'xhs'}`}>{candidate.platform === 'douyin' ? '抖' : '小'}</span><span className="radar-route-badge">{candidate.route || (researchBatch.skip_classification ? '未分类' : '待分类')}</span><small>{candidate.composite_score != null ? `${Math.round(candidate.composite_score * 100)}分` : researchBatch.skip_classification ? candidate.verification_status : candidate.classification_status}</small></header>
                <strong>{candidate.title}</strong>
                {candidate.excerpt && <><small className="radar-summary-label">搜索页摘要</small><p>{candidate.excerpt}</p></>}
                {candidate.jev?.reason && <div className="radar-card-judgement"><span><b>JEV 判断</b>{candidate.jev.reason}</span><span><b>内容支柱</b>{candidate.pillar || '待人工判断'}</span></div>}
                {candidate.risk_flags.length > 0 && <small className="radar-risk-flags">风险：{candidate.risk_flags.join('、')}</small>}
                <footer><span>{[candidate.author, radarMetricText(candidate.metrics)].filter(Boolean).join(' · ')}</span><a href={candidate.url} target="_blank" rel="noreferrer">查看原帖 <ExternalLink /></a></footer>
                {candidate.platform === 'xiaohongshu' && (xhsExtraction
                  ? <XhsExtractionPanel extraction={xhsExtraction} onArchive={resume => archiveXhsPost(xhsExtraction, resume)} onRewrite={setRewriteSource} archiving={archivingId === xhsExtraction.id} />
                  : <div className="radar-result-copy"><small>正文归档：{candidateDetailStatus}</small>{canReadCandidateDetails && <button className="xhs-extract-button" onClick={() => archiveXhsUrl(candidate.url, candidate.title, researchBatch.topic)} disabled={archivingId === candidate.url}>{archivingId === candidate.url ? <LoaderCircle className="spin" /> : <Save />}{archivingId === candidate.url ? '正在建立归档任务' : '保存原文与图片'}</button>}</div>)}
                {candidate.platform === 'douyin' && (douyinExtraction
                  ? <DouyinExtractionPanel extraction={douyinExtraction} extracting={extractingDouyinUrl === candidate.url} onExtract={() => extractDouyinPost(candidate.url, candidate.title, researchBatch.topic)} />
                  : <div className="radar-result-copy"><small>发布文案：{candidateDetailStatus}</small>{canReadCandidateDetails && <button className="xhs-extract-button" onClick={() => extractDouyinPost(candidate.url, candidate.title, researchBatch.topic)} disabled={extractingDouyinUrl === candidate.url}>{extractingDouyinUrl === candidate.url ? <LoaderCircle className="spin" /> : <FileText />}{extractingDouyinUrl === candidate.url ? '正在读取详情' : '读取完整发布文案'}</button>}</div>)}
                {candidate.route === 'A' && <button className="radar-create-from-post" onClick={() => setSelectedAngle(candidate.title)}>{selectedAngle === candidate.title ? <CheckCircle2 /> : <WandSparkles />}{selectedAngle === candidate.title ? '已选为创作主参考' : '选为创作主参考'}</button>}
              </article>
            })}</div>
            {batchCandidates.length < batchCandidateTotal && <button className="secondary-button radar-load-more" onClick={() => loadBatchCandidates(researchBatch.id, batchRoute, batchCandidates.length)}>加载更多（已显示 {batchCandidates.length}/{batchCandidateTotal}）</button>}
          </div>}
        </div> : <div className="radar-path-empty"><Search /><div><strong>先在下方输入一个专业主题</strong><p>默认目标 300 条；系统会按所选人设扩展查询、双平台去重，并把实时核验候选送入原文归档队列，不调用 JEV。</p></div></div>}
      </section>

      {(Object.keys(selectedXhs).length > 0 || archiveBatch) && <section className="xhs-batch-bar">
        <div><strong>{Object.keys(selectedXhs).length > 0 ? `已选择 ${Object.keys(selectedXhs).length} 篇小红书帖子` : '批量归档进度'}</strong>
          <small>{archiveBatch ? `等待 ${archiveBatch.counts.waiting} · 进行 ${archiveBatch.counts.running} · 完整 ${archiveBatch.counts.complete} · 部分 ${archiveBatch.counts.partial} · 暂停 ${archiveBatch.counts.paused} · 失败 ${archiveBatch.counts.failed + archiveBatch.counts.invalid}` : '选择会保存在本机；提交后按顺序逐篇处理。'}</small></div>
        {Object.keys(selectedXhs).length > 0 && <div><button className="secondary-button" onClick={() => setSelectedXhs({})}>清空</button><button className="primary-button" onClick={submitArchiveBatch} disabled={batchSubmitting}>{batchSubmitting ? <LoaderCircle className="spin" /> : <Save />}{batchSubmitting ? '正在加入队列' : '归档选中项'}</button></div>}
      </section>}

      {rewriteSource && <RewriteWorkbench
        workspaceId={workspaceId}
        extraction={rewriteSource}
        onClose={() => setRewriteSource(null)}
        notify={notify}
      />}

      <div className="radar-path-grid">
        <section className="radar-path-card radar-ip-card">
          <div className="radar-path-heading">
            <span className="radar-path-icon"><Bot /></span>
            <div><span className="section-number">01</span><h2>每日 IP 自动热点</h2><p>按你的人设筛选值得看的方向，每张卡清楚展示标题、内容方向、热度依据和创作判断。</p></div>
          </div>
          <div className="radar-path-actions">
            {!dailyMonitor ? <button className="primary-button" onClick={enableDaily} disabled={dailyAction !== ''}>{dailyAction === 'enable' ? <LoaderCircle className="spin" /> : <Sparkles />}{dailyAction === 'enable' ? '正在开启' : '开启每天自动找热点'}</button> : <>
              <span className={`radar-status-chip ${dailyMonitor.enabled ? 'on' : ''}`}><span />{dailyMonitor.enabled ? '已开启 · 每天自动更新' : '已暂停'}</span>
              <button className="secondary-button" onClick={() => toggleMonitor(dailyMonitor)}>{dailyMonitor.enabled ? '暂停' : '恢复'}</button>
              <button className="primary-button" onClick={runDaily} disabled={dailyAction !== '' || serverDailyRefreshing}>{dailyAction === 'run' || serverDailyRefreshing ? <LoaderCircle className="spin" /> : <RefreshCw />}{dailyAction === 'run' || serverDailyRefreshing ? '正在更新' : '现在帮我找热点'}</button>
            </>}
          </div>
          {dailyMonitor?.last_error && <div className="radar-notice"><AlertTriangle /><span>{dailyMonitor.last_error}</span></div>}
          {dailyRun ? <div className="radar-daily-result">
            <div className="radar-daily-result-heading"><div><strong>今天的 IP 方向</strong><small>{dailyFreshnessLabel}</small></div><span>{dailyRecommendations.length || 0} 个方向</span></div>
            <RadarStats value={dailyStats} />
            <DailyVerificationGate
              verification={dailyVerification}
              browserStatus={browserStatus}
              opening={openingBrowser}
              busy={dailyAction === 'run' || serverDailyRefreshing}
              onOpenBrowser={() => openPlatformBrowser(dailyMonitor?.query || '')}
              onRetry={retryDailyVerification}
            />
            <div className="radar-daily-platforms">{(['xiaohongshu', 'douyin'] as const).map(platform => {
              const summary = dailyPlatforms.find(item => item.platform === platform)
              const realtimeVerified = isRealtimeVerifiedPlatform(summary)
              const count = realtimeVerified ? Number(summary?.count || 0) : 0
              const label = platform === 'douyin' ? '抖音' : '小红书'
              const statusText = realtimeVerified
                ? count > 0 ? '本轮已实时读取，按相关性和可见互动数排序' : '本轮已实时核验，没有新的匹配帖子'
                : dailyVerification.phase === 'running' ? '正在实时读取，完成前不显示旧结果'
                  : '待完成本轮实时验证'
              return <div className={realtimeVerified ? 'ready' : 'needs-verification'} key={platform}><span className={`platform-dot ${platform === 'douyin' ? 'dy' : 'xhs'}`}>{platform === 'douyin' ? '抖' : '小'}</span><div><strong>{label} {realtimeVerified ? `${count} 条` : '待验证'}</strong><small>{statusText}</small></div></div>
            })}</div>
            {dailyJev && !['COMPLETED', 'NO_CANDIDATES'].includes(String(dailyJev.status)) && <div className="radar-notice"><AlertTriangle /><span>{dailyJev.message || (dailyJevPending ? '候选已保存；连接并验证 TypeSafe JEV 后，才会生成匹配人设与业务的优先推荐。' : '部分候选尚未完成 JEV 分类；未分类内容不会进入优先推荐，原文归档状态会单独显示。')}</span></div>}
            <div className={`radar-batch-extract ${dailyVerification.phase === 'verified' ? '' : 'waiting-verification'}`}><span>{dailyVerification.phase !== 'verified' ? <AlertTriangle /> : hasAutomaticExtraction ? <LoaderCircle className="spin" /> : <FileText />}{dailyVerification.phase !== 'verified' ? '先完成本轮实时验证' : hasAutomaticExtraction ? '正在读取原帖正文与归档内容' : '当前没有运行中的归档任务'}</span><small>{dailyVerification.phase !== 'verified' ? '验证完成前不会从历史缓存建立原帖归档或改写任务。' : '正文完整度以各条目的核验结果为准，暂停或失败的任务仍需处理。JEV 负责推荐优先级，原文归档独立排队。'}</small></div>
            {dailyRecommendations.length > 0 ? <div className="radar-recommendations">{dailyRecommendations.map((item, index) => {
              const title = recommendationTitle(item)
              const platformLinks = recommendationPlatformSearchLinks(item)
              const persona = typeof item === 'string' ? '' : item.persona
              const pillar = typeof item === 'string' ? '' : item.pillar
              const searchQuery = typeof item === 'string' ? title : item.search_query || title
              const originalPost = recommendationOriginalPost(item)
              const extraction = originalPost?.platform === 'xiaohongshu' ? findXhsExtraction(xhsExtractions, originalPost.url) : undefined
              const sourceItem = originalPost ? dailyRun.items.find(value => value.url === originalPost.url) : undefined
              const favorite = favoriteForItem(sourceItem)
              return <article className="radar-recommendation" key={`${title}-${index}`}>
                <div className="radar-recommendation-kicker"><b>{String(index + 1).padStart(2, '0')}</b><span>{persona || '今日推荐'}</span></div>
                <strong>{title || '今日可看方向'}</strong>
                {pillar && <small className="radar-recommendation-pillar"><b>IP 方向</b>{pillar}</small>}
                <p><b>创作判断</b>{recommendationReason(item) || '结合真实内容与评论，把这个话题改写成贴近受众的内容。'}</p>
                {originalPost && <a className="radar-original-post" href={originalPost.url} target="_blank" rel="noreferrer"><span className={`platform-dot ${originalPost.platform === 'douyin' ? 'dy' : 'xhs'}`}>{originalPost.platform === 'douyin' ? '抖' : '小'}</span><span><b>{originalPost.label}</b><small>{[radarMetricText(typeof item === 'string' ? undefined : item.visible_metrics), '查看真实原帖'].filter(Boolean).join(' · ')}</small></span><ExternalLink /></a>}
                {sourceItem && <button className={`radar-favorite-button ${favorite ? 'saved' : ''}`} onClick={() => toggleFavorite(sourceItem)}>{favorite ? <Heart fill="currentColor" /> : <Heart />}{favorite ? '已收藏' : '收藏原帖'}</button>}
                {originalPost?.platform === 'xiaohongshu' && <button className={`xhs-select-button ${selectedXhs[originalPost.url] ? 'selected' : ''}`} onClick={() => toggleXhsSelection(originalPost.url, title, searchQuery)}>{selectedXhs[originalPost.url] ? <CheckCircle2 /> : <Check />}{selectedXhs[originalPost.url] ? '已选中归档' : '选择归档'}</button>}
                {originalPost?.platform === 'xiaohongshu' && !extraction && <button className="xhs-extract-button" onClick={() => archiveXhsUrl(originalPost.url, title, searchQuery)} disabled={archivingId === originalPost.url}>{archivingId === originalPost.url ? <LoaderCircle className="spin" /> : <Save />}{archivingId === originalPost.url ? '正在建立归档任务' : '保存原图与正文'}</button>}
                {originalPost?.platform === 'xiaohongshu' && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(originalPost.url, title, searchQuery, false, true)} disabled={extractingUrl === originalPost.url}>{extractingUrl === originalPost.url ? <LoaderCircle className="spin" /> : <RefreshCw />}{extractingUrl === originalPost.url ? '正在重试' : xhsRetryLabel(extraction)}</button>}
                <XhsExtractionPanel extraction={extraction} onArchive={extraction ? resume => archiveXhsPost(extraction, resume) : undefined} onRewrite={setRewriteSource} archiving={archivingId === extraction?.id} />
                <footer><button onClick={() => onCreateFromTopic({ topic: title || dailyMonitor?.query || '今天的 IP 热点', sourceUrls: recommendationSourceUrls(item, platformLinks), searchQuery, platformSearches: platformLinks })}><WandSparkles />用这个方向创作</button></footer>
              </article>
            })}</div> : <div className="radar-path-empty"><Search /><div><strong>{dailyVerification.phase === 'action_required' ? '先完成双平台实时验证' : dailyVerification.phase === 'running' ? '正在读取双平台内容' : dailyJevPending ? '内容已读取，等待连接 JEV 分类' : dailyVerifiedItems.length > 0 ? '本轮没有通过 JEV 的 A 类候选' : '本轮没有新的匹配内容'}</strong><p>{dailyVerification.phase === 'action_required' ? '请在采集浏览器完成登录或安全验证，再点击“重新验证并找热点”。' : dailyVerification.phase === 'running' ? '验证完成后才会显示新的 IP 方向和原帖。' : dailyJevPending ? '到设置中连接并验证 JEV Key；已采集候选会保留。' : dailyVerifiedItems.length > 0 ? '候选仍保留在来源列表，可查看 JEV A/B/C 判断；A 类用于优先推荐，正文归档独立处理实时候选。' : '已核验过的平台不会拿旧帖子重新填充今日方向。'}</p></div></div>}
            {dailyVerifiedItems.length > 0 && <details className="radar-visible-sources"><summary>查看本轮已实时验证的 {dailyVerifiedItems.length} 条内容 <ChevronDown /></summary><div className="radar-item-grid">{dailyVerifiedItems.map(item => { const extraction = item.platform === 'xiaohongshu' ? findXhsExtraction(xhsExtractions, item.url) : undefined; const douyinExtraction = item.platform === 'douyin' ? findDouyinExtraction(douyinExtractions, item.url) : undefined; return <RadarItemCard key={item.id} item={item} extraction={extraction} douyinExtraction={douyinExtraction} extracting={extractingUrl === item.url} extractingDouyin={extractingDouyinUrl === item.url} archiving={archivingId === extraction?.id || archivingId === item.url} selected={Boolean(selectedXhs[item.url])} favorite={favoriteForItem(item)} onToggleFavorite={() => toggleFavorite(item)} onRewrite={setRewriteSource} onCreate={() => onCreateFromTopic({ topic: item.title, sourceUrls: [item.url], searchQuery: String(item.raw?.search_query || dailyMonitor?.query || item.title) })} onToggleSelect={(url, title) => toggleXhsSelection(url, title, dailyMonitor?.query || title)} onExtract={(url, title) => extractXhsPost(url, title, dailyMonitor?.query || title, false, Boolean(extraction))} onExtractDouyin={(url, title) => extractDouyinPost(url, title, dailyMonitor?.query || title)} onArchive={(saved, resume) => archiveXhsPost(saved, resume)} onArchiveUrl={(url, title) => archiveXhsUrl(url, title, dailyMonitor?.query || title)} /> })}</div></details>}
          </div> : <div className="radar-path-empty"><Sparkles /><div><strong>先让它认识你的内容方向</strong><p>开启后会每天自动观察；不需要你每天想一个新的搜索词。</p></div></div>}
        </section>

        <section className="radar-path-card radar-topic-card">
          <div className="radar-path-heading">
            <span className="radar-path-icon peach"><Search /></span>
            <div><span className="section-number">02</span><h2>主题批量研究后创作</h2><p>输入主题后先建立候选池；实时核验的候选正文独立排队归档，JEV 再按人设和业务分层，A 类优先推荐、B 类复核、C 类不推荐。</p></div>
          </div>
          <div className="radar-topic-form">
            <input value={topicQuery} onChange={event => setTopicQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') searchTopic() }} placeholder="例如：为什么越来越多女生不想相亲" aria-label="输入想搜索的主题" />
            <div className="radar-format-switch" role="group" aria-label="选择创作格式">
              <button className={searchMode === 'xiaohongshu_ip' ? 'selected' : ''} onClick={() => setSearchMode('xiaohongshu_ip')}><span className="platform-dot xhs">小</span>小红书文案</button>
              <button className={searchMode === 'douyin_emotion' ? 'selected' : ''} onClick={() => setSearchMode('douyin_emotion')}><span className="platform-dot dy">抖</span>抖音口播</button>
            </div>
            <div className="radar-topic-actions"><button className="secondary-button" onClick={searchTopic} disabled={searchingTopic || startingResearchBatch || creatingFromTopic || researchBatchRunning}>{searchingTopic || startingResearchBatch ? <LoaderCircle className="spin" /> : <Search />}{searchingTopic || startingResearchBatch ? '正在启动' : '启动批量研究'}</button><button className="primary-button" onClick={createFromTopicSearch} disabled={searchingTopic || creatingFromTopic || researchBatchRunning}>{creatingFromTopic ? <LoaderCircle className="spin" /> : <WandSparkles />}{creatingFromTopic ? '正在开始创作' : '用 A 类内容创作'}</button></div>
          </div>
          <div className="radar-full-copy-guide"><FileText /><div><strong>正文归档与 JEV 分层分开</strong><p>候选先按真实平台 ID 去重；所有本轮实时核验的候选都可逐篇排入正文、图片和抖音发布文案读取，JEV A/B/C 负责推荐优先级，不会把 B/C 的正文挡掉。平台要求验证时保留等待状态，由本人处理后续跑。</p></div></div>
          {topicResult && <div className="radar-topic-result">
            <div className="radar-daily-result-heading"><div><strong>“{topicResult.topic || topicQuery}” 的可讲方向</strong><small>{topicResult.live_verified ? `双平台实时读取 ${topicResult.visible_source_count || 0} 条内容` : '本轮未完成双平台实时验证，不能生成创作方向'}</small></div><span>{topicAngles.length} 个主题</span></div>
            <RadarStats value={topicStats} />
            {topicAngles.length > 0 ? <div className="topic-angle-list">{topicAngles.map((angle, index) => {
              const title = recommendationTitle(angle)
              return <button key={`${title}-${index}`} className={selectedAngle === title ? 'selected' : ''} onClick={() => setSelectedAngle(title)}><span>{selectedAngle === title ? <CheckCircle2 /> : index + 1}</span><div><strong>{title || '可讲内容方向'}</strong><p>{recommendationReason(angle) || '选中后会把这个角度和搜索资料一起带进创作。'}</p></div></button>
            })}</div> : <p className="radar-result-copy">{topicResult.jev_classification?.status === 'AWAITING_JEV_KEY' ? '实时搜索资料已保存；连接并验证 JEV 后，才会按当前人设和业务生成优先创作方向。' : topicResult.jev_classification?.status === 'PARTIAL' || topicResult.jev_classification?.status === 'FAILED' ? 'JEV 分类未完整完成；未分类内容不会作为优先创作方向。' : '本轮没有通过 JEV 的 A 类内容；可查看来源或换一个更具体的主题。'}</p>}
            {!topicResult.live_verified && <div className="radar-notice"><AlertTriangle /><span>{topicResult.warning || '小红书和抖音需分别完成本轮实时读取；当前不会把部分结果或历史内容带入创作。'}</span></div>}
            {topicResult.jev_classification && !['COMPLETED', 'NO_CANDIDATES'].includes(String(topicResult.jev_classification.status)) && <div className="radar-notice"><AlertTriangle /><span>{topicResult.jev_classification.message || topicResult.warning || 'JEV 分类未完成，优先推荐暂不可用。'}</span></div>}
            {topicSources.length > 0 && <details className="radar-visible-sources" open><summary>查看实际找到的 {topicSources.length} 条平台内容 <ChevronDown /></summary><div className="radar-topic-source-grid">{topicSources.map((source, index) => {
              const isXhs = source.metadata?.platform === 'xiaohongshu'
              const isDouyin = source.metadata?.platform === 'douyin'
              const extraction = isXhs ? findXhsExtraction(xhsExtractions, source.url) || source.metadata?.xhs_extraction : undefined
              const douyinExtraction = isDouyin ? findDouyinExtraction(douyinExtractions, source.url) : undefined
              const favorite = isXhs ? favoriteForTopicSource(source) : undefined
              return <article key={`${source.url}-${index}`} className="radar-topic-source">
                <span>{isXhs ? '小红书' : source.metadata?.platform === 'douyin' ? '抖音' : '平台原文'}</span>{radarJevBadge(source) && <span className="radar-route-badge">{radarJevBadge(source)}</span>}
                <strong>{source.title}</strong>
                <small className="radar-summary-label">搜索页摘要</small>
                <p>{source.excerpt}</p>
                <div className="radar-card-judgement"><span><b>与账号关系</b>{String(source.metadata?.account_relation || `与“${topicResult.topic || topicQuery}”直接相关`)}</span><span><b>可转化方向</b>{String(source.metadata?.content_direction || '拆出真实顾虑与冲突，再给出当前相亲 IP 的判断和行动建议。')}</span></div>
                <footer><small>{[source.metadata?.author, radarMetricText(source.metadata?.metrics)].filter(Boolean).join(' · ')}</small><a className="radar-inline-original" href={source.url} target="_blank" rel="noreferrer">查看原文 <ExternalLink /></a></footer>
                {isXhs && <button className={`radar-favorite-button ${favorite ? 'saved' : ''}`} onClick={() => toggleTopicSourceFavorite(source)}>{favorite ? <Heart fill="currentColor" /> : <Heart />}{favorite ? '已收藏' : '收藏原帖'}</button>}
                {isXhs && <button className={`xhs-select-button ${selectedXhs[source.url] ? 'selected' : ''}`} onClick={() => toggleXhsSelection(source.url, source.title, topicResult.topic || topicQuery)}>{selectedXhs[source.url] ? <CheckCircle2 /> : <Check />}{selectedXhs[source.url] ? '已选中归档' : '选择归档'}</button>}
                {isXhs && !extraction && <button className="xhs-extract-button" onClick={() => archiveXhsUrl(source.url, source.title, topicResult.topic || topicQuery)} disabled={archivingId === source.url}>{archivingId === source.url ? <LoaderCircle className="spin" /> : <Save />}{archivingId === source.url ? '正在建立归档任务' : '保存原图与正文'}</button>}
                {isXhs && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(source.url, source.title, topicResult.topic || topicQuery, false, true)} disabled={extractingUrl === source.url}>{extractingUrl === source.url ? <LoaderCircle className="spin" /> : <RefreshCw />}{extractingUrl === source.url ? '正在重试' : xhsRetryLabel(extraction)}</button>}
                <XhsExtractionPanel extraction={extraction} onArchive={extraction ? resume => archiveXhsPost(extraction, resume) : undefined} onRewrite={setRewriteSource} archiving={archivingId === extraction?.id} />
                {isDouyin && !douyinExtraction && <button className="xhs-extract-button" onClick={() => extractDouyinPost(source.url, source.title, topicResult.topic || topicQuery)} disabled={extractingDouyinUrl === source.url}>{extractingDouyinUrl === source.url ? <LoaderCircle className="spin" /> : <FileText />}{extractingDouyinUrl === source.url ? '正在读取详情' : '读取完整发布文案'}</button>}
                {isDouyin && <DouyinExtractionPanel extraction={douyinExtraction} extracting={extractingDouyinUrl === source.url} onExtract={() => extractDouyinPost(source.url, source.title, topicResult.topic || topicQuery)} />}
                <button className="radar-create-from-post" onClick={() => onCreateFromTopic({ topic: source.title, sourceUrls: [source.url], searchQuery: topicResult.topic || topicQuery })}><WandSparkles />带入创作</button>
              </article>
            })}</div></details>}
          </div>}
        </section>
      </div>

      {favorites.length > 0 && <details className="radar-favorite-library" open>
        <summary><span><Heart fill="currentColor" />收藏的原帖（{favorites.length}）</span><small>刷新不会丢失；可从这里继续查看或改写</small><ChevronDown /></summary>
        <div>{favorites.map(item => {
          const extraction = item.platform === 'xiaohongshu' ? findXhsExtraction(xhsExtractions, item.url) : undefined
          return <article key={item.id}><span className={`platform-dot ${item.platform === 'douyin' ? 'dy' : item.platform === 'xiaohongshu' ? 'xhs' : 'web'}`}>{item.platform === 'douyin' ? '抖' : item.platform === 'xiaohongshu' ? '小' : '热'}</span><div><strong>{item.title}</strong><small>{[item.author, radarMetricText(item.metrics)].filter(Boolean).join(' · ')}</small></div><a href={item.url} target="_blank" rel="noreferrer">查看原帖 <ExternalLink /></a>{extraction?.archive?.body_raw != null && extraction.archive.verification_status === 'VERIFIED' && <button onClick={() => setRewriteSource(extraction)}><WandSparkles />按这篇改写</button>}<button className="remove" onClick={() => removeFavorite(item)}><Heart fill="currentColor" />取消收藏</button></article>
        })}</div>
      </details>}

      {savedXhsExtractions.length > 0 && <details className="xhs-extraction-library xhs-extraction-library-wide">
        <summary><span>小红书页面与归档库（当前载入 {savedXhsExtractions.length} 篇 · 其中正文已验证 {savedXhsWithExactBody} 篇{savedXhsPending ? ` · 处理中 ${savedXhsPending} 篇` : ''}）</span><small>显示最近至多 500 条；搜索摘要、旧版页面文本、逐字原文与图片归档状态分开标记</small><ChevronDown /></summary>
        <div>{savedXhsExtractions.map(extraction => <article key={extraction.id}>
          <header><div><span className="platform-dot xhs">小</span><strong>{extraction.title || '小红书原帖'}</strong></div><a href={extraction.url} target="_blank" rel="noreferrer">平台原帖 <ExternalLink /></a></header>
          {xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(extraction.url, extraction.title, extraction.search_keyword, false, true)} disabled={extractingUrl === extraction.url}>{extractingUrl === extraction.url ? <LoaderCircle className="spin" /> : <RefreshCw />}重新尝试</button>}
          <XhsExtractionPanel extraction={extraction} onArchive={resume => archiveXhsPost(extraction, resume)} onRewrite={setRewriteSource} archiving={archivingId === extraction.id} />
        </article>)}</div>
      </details>}

      {savedDouyinExtractions.length > 0 && <details className="xhs-extraction-library xhs-extraction-library-wide douyin-extraction-library">
        <summary><span>抖音文案库（当前载入 {savedDouyinExtractions.length} 条 · 其中完整发布文案 {savedDouyinWithFullCopy} 条{savedDouyinTranscriptReview > 0 ? ` · 口播待核对 ${savedDouyinTranscriptReview} 条` : ''}{savedDouyinTranscriptUnknown > 0 ? ` · 口播未评估 ${savedDouyinTranscriptUnknown} 条` : ''}）</span><small>显示最近至多 500 条；发布文案与平台字幕、口播转写分开展示，未核验完整的记录保留为部分结果</small><ChevronDown /></summary>
        <div>{savedDouyinExtractions.map(extraction => <article key={extraction.id}>
          <header><div><span className="platform-dot dy">抖</span><strong>{extraction.title || '抖音视频'}</strong></div><a href={extraction.url} target="_blank" rel="noreferrer">平台原帖 <ExternalLink /></a></header>
          <DouyinExtractionPanel extraction={extraction} extracting={extractingDouyinUrl === extraction.url} onExtract={() => extractDouyinPost(extraction.url, extraction.title, extraction.search_keyword)} />
        </article>)}</div>
      </details>}

    </div>
  )
}

function RadarItemCard({ item, extraction, douyinExtraction, extracting, extractingDouyin, archiving, selected, favorite, onToggleFavorite, onRewrite, onCreate, onToggleSelect, onExtract, onExtractDouyin, onArchive, onArchiveUrl }: {
  item: TopicRadarItem
  extraction?: XiaohongshuExtraction
  douyinExtraction?: DouyinExtraction
  extracting: boolean
  extractingDouyin: boolean
  archiving: boolean
  selected: boolean
  favorite?: RadarFavorite
  onToggleFavorite?: () => void
  onRewrite?: (extraction: XiaohongshuExtraction) => void
  onCreate: () => void
  onToggleSelect: (url: string, title: string) => void
  onExtract: (url: string, title: string) => void
  onExtractDouyin: (url: string, title: string) => void
  onArchive: (extraction: XiaohongshuExtraction, resumeAfterUserAction?: boolean) => void
  onArchiveUrl: (url: string, title: string) => void
}) {
  const metricEntries = Object.entries(item.metrics || {}).filter(([, value]) => value !== null && value !== undefined && value !== '')
  const publicWeb = item.platform === 'web'
  const dotClass = item.platform === 'xiaohongshu' ? 'xhs' : publicWeb ? 'web' : 'dy'
  const livePlatformContent = item.source_mode === 'visible_browser' || item.source_mode === 'managed_background_browser' || item.source_mode === 'browser_verified' || item.source_mode === 'verified_browser'
  const platformLabel = item.platform === 'xiaohongshu' ? (livePlatformContent ? '小红书实时内容' : '小红书公开索引') : publicWeb ? '公开趋势线索' : livePlatformContent ? '抖音实时内容' : '抖音公开索引'
  const dotText = item.platform === 'xiaohongshu' ? '小' : publicWeb ? '热' : '抖'
  return <article className="radar-item-card">
    <div className="radar-item-meta"><span className={`platform-dot ${dotClass}`}>{dotText}</span><span>{platformLabel}</span>{radarJevBadge(item) && <span className={`radar-route-badge route-${String(item.raw?.jev && typeof item.raw.jev === 'object' ? (item.raw.jev as Record<string, unknown>).route || 'pending' : 'pending').toLowerCase()}`}>{radarJevBadge(item)}</span>}{item.is_new && <b>NEW</b>}</div>
    <h3>{item.title}</h3>
    {item.excerpt && <><small className="radar-summary-label">搜索页摘要</small><p>{item.excerpt}</p></>}
    <div className="radar-card-judgement"><span><b>与账号关系</b>{String(item.raw?.account_relation || '与当前相亲账号的受众问题相关')}</span><span><b>可转化方向</b>{String(item.raw?.content_direction || '从原帖处境提炼冲突、判断与可执行建议')}</span></div>
    <footer><span>{item.author || '平台内容'}{item.published_at ? ` · ${formatDate(item.published_at)}` : ''}</span>{metricEntries.length > 0 && <small>{metricEntries.slice(0, 3).map(([key, value]) => `${key === 'likes' ? '赞' : key === 'comments' ? '评' : key === 'shares' ? '转' : key === 'plays' ? '播' : key === 'visible_engagement' ? '可见热度' : key}${value}`).join(' · ')}</small>}{item.url && <a href={item.url} target="_blank" rel="noreferrer" aria-label="打开内容"><ExternalLink /></a>}</footer>
    {item.url && <a className="radar-inline-original" href={item.url} target="_blank" rel="noreferrer">查看原文 <ExternalLink /></a>}
    {onToggleFavorite && <button className={`radar-favorite-button ${favorite ? 'saved' : ''}`} onClick={onToggleFavorite}>{favorite ? <Heart fill="currentColor" /> : <Heart />}{favorite ? '已收藏' : '收藏原帖'}</button>}
    {item.platform === 'xiaohongshu' && item.url && <button className={`xhs-select-button ${selected ? 'selected' : ''}`} onClick={() => onToggleSelect(item.url, item.title)}>{selected ? <CheckCircle2 /> : <Check />}{selected ? '已选中归档' : '选择归档'}</button>}
    {item.platform === 'xiaohongshu' && item.url && !extraction && <button className="xhs-extract-button" onClick={() => onArchiveUrl(item.url, item.title)} disabled={archiving}>{archiving ? <LoaderCircle className="spin" /> : <Save />}{archiving ? '正在建立归档任务' : '保存原图与正文'}</button>}
    {item.platform === 'xiaohongshu' && item.url && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => onExtract(item.url, item.title)} disabled={extracting}>{extracting ? <LoaderCircle className="spin" /> : <RefreshCw />}{extracting ? '正在重试' : xhsRetryLabel(extraction)}</button>}
    <XhsExtractionPanel extraction={extraction} onArchive={extraction ? resume => onArchive(extraction, resume) : undefined} onRewrite={onRewrite} archiving={archiving} />
    {item.platform === 'douyin' && item.url && !douyinExtraction && <button className="xhs-extract-button" onClick={() => onExtractDouyin(item.url, item.title)} disabled={extractingDouyin}>{extractingDouyin ? <LoaderCircle className="spin" /> : <FileText />}{extractingDouyin ? '正在读取完整发布文案' : '读取完整发布文案'}</button>}
    <DouyinExtractionPanel extraction={douyinExtraction} extracting={extractingDouyin} onExtract={() => onExtractDouyin(item.url, item.title)} />
    <button className="radar-create-from-post" onClick={onCreate}><WandSparkles />带入创作</button>
  </article>
}

function radarMetricText(metrics?: Record<string, number | string | null>) {
  return Object.entries(metrics || {}).filter(([, value]) => value !== null && value !== undefined && value !== '').slice(0, 2).map(([key, value]) => `${key === 'likes' ? '赞' : key === 'comments' ? '评' : key === 'shares' ? '转' : key === 'plays' ? '播' : key === 'visible_engagement' ? '可见热度' : key}${value}`).join(' · ')
}

function WorksPage({ tasks, lifeCases, detail, contentTypes, onOpen, onRefresh, onCreated, onCasesChanged, onDuplicateCase, onSettings, notify }: {
  tasks: Task[]
  lifeCases: LifeCase[]
  detail: TaskDetail | null
  contentTypes: ContentType[]
  onOpen: (id: string) => Promise<void>
  onRefresh: () => Promise<void>
  onCreated: (value: TaskDetail) => Promise<void>
  onCasesChanged: () => Promise<void>
  onDuplicateCase: (value: LifeCase) => void
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null)
  const filtered = useMemo(() => tasks.filter(task => {
    const matchQuery = !query || task.topic.toLowerCase().includes(query.toLowerCase())
    const matchFilter = filter === 'all' || (filter === 'final' ? task.status === 'FINAL_READY' : ['NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED'].includes(task.status))
    return matchQuery && matchFilter
  }), [tasks, query, filter])
  const filteredCases = useMemo(() => lifeCases.filter(item => !query || `${item.title} ${item.note_text}`.toLowerCase().includes(query.toLowerCase())), [lifeCases, query])
  const selectedCase = lifeCases.find(item => item.id === selectedCaseId) || null

  const regenerate = async () => {
    if (!detail) return
    try {
      const value = await api<TaskDetail>(`/api/copy-tasks/${detail.task.id}/regenerate`, { method: 'POST' })
      await onCreated(value)
      notify('已创建一条新的生成任务')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  return (
    <div className="works-page">
      <section className="page-heading"><span className="eyebrow">所有创作都在这里</span><h1>作品记录</h1><p>打开、修改或重新生成，不会覆盖之前的版本。</p></section>
      <div className="works-layout">
        <aside className="work-list-panel">
          <div className="list-tools">
            <label><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索作品" /></label>
            <div>
              <button className={filter === 'all' ? 'active' : ''} onClick={() => { setFilter('all'); setSelectedCaseId(null) }}>全部</button>
              <button className={filter === 'final' ? 'active' : ''} onClick={() => { setFilter('final'); setSelectedCaseId(null) }}>已定稿</button>
              <button className={filter === 'problem' ? 'active' : ''} onClick={() => { setFilter('problem'); setSelectedCaseId(null) }}>需处理</button>
              <button className={filter === 'life' ? 'active' : ''} onClick={() => { setFilter('life'); setSelectedCaseId(null) }}>生活案例</button>
            </div>
          </div>
          <div className="work-list">
            {filter === 'life' ? <>
              {filteredCases.length === 0 && <div className="empty-list"><Camera /><p>还没有保存生活案例</p></div>}
              {filteredCases.map(item => (
                <button className={selectedCaseId === item.id ? 'active' : ''} key={item.id} onClick={async () => {
                  setSelectedCaseId(item.id)
                  if (item.latest_task_id) await onOpen(item.latest_task_id)
                }}>
                  <span className={`task-dot ${item.archived ? '' : (item.latest_task_status || '').toLowerCase()}`} />
                  <div><strong>{item.title}</strong><small>{item.archived ? '已归档' : item.latest_task_status ? statusNames[item.latest_task_status] || item.latest_task_status : '仅保存，尚未生成'}</small></div>
                  <time>{item.occurred_at?.slice(5, 10) || formatDate(item.created_at)}</time>
                </button>
              ))}
            </> : <>
              {filtered.length === 0 && <div className="empty-list"><FileText /><p>还没有符合条件的作品</p></div>}
              {filtered.map(task => (
                <button className={detail?.task.id === task.id ? 'active' : ''} key={task.id} onClick={() => { setSelectedCaseId(null); onOpen(task.id) }}>
                  <span className={`task-dot ${task.status.toLowerCase()}`} />
                  <div><strong>{task.topic}</strong><small>{taskProductLabel(task)} · {statusNames[task.status] || task.status}</small></div>
                  <time>{formatDate(task.created_at)}</time>
                </button>
              ))}
            </>}
          </div>
        </aside>
        <section className="work-detail">
          {filter === 'life' ? selectedCase ? (
            <LifeCaseRecord
              caseItem={selectedCase}
              taskDetail={detail?.task.id === selectedCase.latest_task_id ? detail : null}
              onRefreshTask={onRefresh}
              onTaskCreated={onCreated}
              onCasesChanged={onCasesChanged}
              onDuplicate={() => onDuplicateCase(selectedCase)}
              onDeleted={() => setSelectedCaseId(null)}
              onSettings={onSettings}
              notify={notify}
            />
          ) : <div className="empty-detail"><Camera /><h2>选择一条生活案例</h2><p>图片、原始记录和三版朋友圈会显示在这里。</p></div> : !detail ? <div className="empty-detail"><FileText /><h2>选择一条作品</h2><p>定稿、来源和主编意见会显示在这里。</p></div> : <>
            <TaskWorkspace detail={detail} onRefresh={onRefresh} onSettings={onSettings} notify={notify} />
            <button className="regenerate-button" onClick={regenerate}><RotateCcw />按相同要求重新生成</button>
          </>}
        </section>
      </div>
    </div>
  )
}

function LifeCaseRecord({ caseItem, taskDetail, onRefreshTask, onTaskCreated, onCasesChanged, onDuplicate, onDeleted, onSettings, notify }: {
  caseItem: LifeCase
  taskDetail: TaskDetail | null
  onRefreshTask: () => Promise<void>
  onTaskCreated: (value: TaskDetail) => Promise<void>
  onCasesChanged: () => Promise<void>
  onDuplicate: () => void
  onDeleted: () => void
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [working, setWorking] = useState('')

  const generate = async () => {
    setWorking('generate')
    try {
      const value = await api<TaskDetail>(`/api/life-cases/${caseItem.id}/generate?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, { method: 'POST' })
      await onTaskCreated(value)
      notify(value.task.status === 'NEEDS_MODEL' ? '任务已保留，连接模型后继续' : '三名数字员工已经开始工作')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  const setArchived = async (archived: boolean) => {
    setWorking('archive')
    try {
      await api<LifeCase>(`/api/life-cases/${caseItem.id}?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, jsonBody('PATCH', { archived }))
      await onCasesChanged()
      notify(archived ? '生活案例已归档，原图仍保留' : '生活案例已恢复')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  const permanentlyDelete = async () => {
    if (!window.confirm('永久删除后，案例文字和本地原图无法恢复；已生成的作品会保留。确定删除吗？')) return
    setWorking('delete')
    try {
      await api(`/api/life-cases/${caseItem.id}?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, { method: 'DELETE' })
      onDeleted()
      await onCasesChanged()
      notify('生活案例和本地原图已永久删除')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  return (
    <div className="life-case-record">
      <div className="case-record-actions">
        <div><span className="eyebrow">生活案例记录</span><h2>{caseItem.title}</h2></div>
        <div>
          <button onClick={onDuplicate}><Copy />复制为新案例</button>
          <button disabled={Boolean(working)} onClick={() => setArchived(!caseItem.archived)}>{caseItem.archived ? <ArchiveRestore /> : <Archive />}{caseItem.archived ? '恢复' : '归档'}</button>
          <button className="danger-button" disabled={Boolean(working)} onClick={permanentlyDelete}><Trash2 />永久删除</button>
        </div>
      </div>
      <LifeCaseMaterial caseItem={caseItem} />
      {!caseItem.archived && !taskDetail && <div className="case-generate-callout"><div><strong>素材已经安全保存</strong><p>现在可以交给研究员、文案员工和主编，生成三种朋友圈角度。</p></div><button className="primary-button" disabled={Boolean(working)} onClick={generate}>{working === 'generate' ? <LoaderCircle className="spin" /> : <Sparkles />}生成三版文案</button></div>}
      {taskDetail && <TaskWorkspace detail={taskDetail} onRefresh={onRefreshTask} onSettings={onSettings} notify={notify} showSourceCase={false} />}
    </div>
  )
}

function SettingsPage({ workspaceId, modelStatus, skills, onUpdated, notify }: {
  workspaceId: string
  modelStatus: Bootstrap['model_status']
  skills: SkillStatus[]
  onUpdated: () => Promise<Bootstrap>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [settings, setSettings] = useState<LocalProxySettings | null>(null)
  const [deepseekSettings, setDeepseekSettings] = useState<DeepSeekSettings | null>(null)
  const [jevSettings, setJevSettings] = useState<JevSettings | null>(null)
  const [profile, setProfile] = useState<WorkspaceProfile | null>(null)
  const [proxyBaseUrl, setProxyBaseUrl] = useState('http://127.0.0.1:8787/v1')
  const [model, setModel] = useState('gpt-5.6-luna')
  const [deepseekModel, setDeepseekModel] = useState('deepseek-v4-flash')
  const [deepseekApiKey, setDeepseekApiKey] = useState('')
  const [jevApiKey, setJevApiKey] = useState('')
  const [commonInfo, setCommonInfo] = useState('')
  const [testing, setTesting] = useState(false)
  const [savingDeepseek, setSavingDeepseek] = useState(false)
  const [savingJev, setSavingJev] = useState(false)
  const [savingInfo, setSavingInfo] = useState(false)

  const load = useCallback(async () => {
    const [localProxy, currentDeepseek, currentJev, currentProfile] = await Promise.all([
      api<LocalProxySettings>('/api/settings/local-proxy'),
      api<DeepSeekSettings>('/api/settings/deepseek'),
      api<JevSettings>(`/api/settings/jev?workspace_id=${encodeURIComponent(workspaceId)}`),
      api<WorkspaceProfile>(`/api/workspaces/${workspaceId}/profile`),
    ])
    setSettings(localProxy)
    setProxyBaseUrl(localProxy.base_url || 'http://127.0.0.1:8787/v1')
    setModel(localProxy.model.model || 'gpt-5.6-luna')
    setDeepseekSettings(currentDeepseek)
    setDeepseekModel(currentDeepseek.model.model || 'deepseek-v4-flash')
    setJevSettings(currentJev)
    setProfile(currentProfile)
    setCommonInfo(String(currentProfile.profile?.common_info || ''))
  }, [workspaceId])

  useEffect(() => { load().catch(error => notify((error as Error).message, 'error')) }, [load, notify])

  const saveConnection = async () => {
    if (!proxyBaseUrl.trim() || !model.trim()) {
      notify('请填写本机代理地址和模型名称', 'error')
      return
    }
    setTesting(true)
    try {
      const result = await api<LocalProxySettings>('/api/settings/local-proxy', jsonBody('PUT', { base_url: proxyBaseUrl, model }))
      setSettings(result)
      await onUpdated()
      notify(result.search.status === 'fallback' ? '本机代理已连接，热点将使用多个公开搜索源与资料库' : '本机代理连接检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setTesting(false) }
  }

  const recheck = async () => {
    setTesting(true)
    try {
      const result = await api<LocalProxySettings>('/api/settings/local-proxy/recheck', { method: 'POST' })
      setSettings(result)
      await onUpdated()
      notify('重新检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setTesting(false) }
  }

  const saveDeepseek = async () => {
    setSavingDeepseek(true)
    try {
      const result = await api<DeepSeekSettings>('/api/settings/deepseek', jsonBody('PUT', {
        model: deepseekModel,
        ...(deepseekApiKey.trim() ? { api_key: deepseekApiKey.trim() } : {}),
      }))
      setDeepseekSettings(result)
      setDeepseekApiKey('')
      await onUpdated()
      notify('DeepSeek 已验证并启用，三名员工将使用它生成文案')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setSavingDeepseek(false) }
  }

  const recheckDeepseek = async () => {
    setSavingDeepseek(true)
    try {
      const result = await api<DeepSeekSettings>('/api/settings/deepseek/recheck', { method: 'POST' })
      setDeepseekSettings(result)
      await onUpdated()
      notify('DeepSeek 重新检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setSavingDeepseek(false) }
  }

  const saveJev = async () => {
    setSavingJev(true)
    try {
      const result = await api<JevSettings>('/api/settings/jev', jsonBody('PUT', {
        workspace_id: workspaceId,
        ...(jevApiKey.trim() ? { api_key: jevApiKey.trim() } : {}),
        enabled: true,
      }))
      setJevSettings(result)
      setJevApiKey('')
      notify('JEV 已验证；批量候选池会自动进入分类与 A/B/C 分流')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
    } finally { setSavingJev(false) }
  }

  const recheckJev = async () => {
    setSavingJev(true)
    try {
      const result = await api<JevSettings>('/api/settings/jev/recheck', jsonBody('POST', {
        workspace_id: workspaceId, enabled: true,
      }))
      setJevSettings(result)
      notify('JEV 连接重新检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
    } finally { setSavingJev(false) }
  }

  const saveInfo = async () => {
    setSavingInfo(true)
    try {
      const value = await api<WorkspaceProfile>(`/api/workspaces/${workspaceId}/profile`, jsonBody('PATCH', { fields: { common_info: commonInfo } }))
      setProfile(value)
      notify('常用信息已保存')
    } catch (error) { notify((error as Error).message, 'error') } finally { setSavingInfo(false) }
  }

  return (
    <div className="settings-page">
      <section className="page-heading"><span className="eyebrow">一次设置，三名员工共用</span><h1>设置</h1><p>这里只保留真正影响文案生成的内容。</p></section>
      <div className="settings-stack">
        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon purple"><Bot /></span><div><h2>本机 OpenAI OAuth 代理</h2><p>质量模式：研究、写稿与主编按同一授权链路工作，默认使用 Luna 的最高推理强度。</p></div><StatusDot status={settings?.model.status || modelStatus.status} /></div>
          <div className="connection-summary">
            <div><span>模型生成</span><strong className={settings?.model.status === 'verified' ? 'success-text' : 'warning-text'}>{settings?.model.message || '正在读取'}</strong></div>
            <div><span>联网搜索</span><strong className={settings?.search.status === 'verified' || settings?.search.status === 'fallback' ? 'success-text' : 'warning-text'}>{settings?.search.message || '正在读取'}</strong></div>
            <div><span>最后检测</span><strong>{settings?.checked_at ? formatDate(settings.checked_at) : '尚未检测'}</strong></div>
          </div>
          <div className="connection-form">
            <label className="key-field"><span>代理根地址</span><input value={proxyBaseUrl} onChange={event => setProxyBaseUrl(event.target.value)} placeholder="http://127.0.0.1:8787/v1" /></label>
            <label><span>模型名称</span><input value={model} onChange={event => setModel(event.target.value)} placeholder="由本机代理提供，例如 gpt-5.6-luna" /></label>
            <button className="primary-button" onClick={saveConnection} disabled={testing}>{testing ? <LoaderCircle className="spin" /> : <Wifi />}{testing ? '正在测试代理' : '测试并保存'}</button>
            {settings && <button className="secondary-button" onClick={recheck} disabled={testing}><RefreshCw />重新检测</button>}
          </div>
          <p className="security-note"><ShieldCheck />这个连接不读取或保存 OAuth Token；登录与授权只在本机代理中完成。点击“测试并保存”会重新启用本机代理。</p>
        </section>

        <section className="settings-card provider-card deepseek-card">
          <div className="settings-title"><span className="settings-icon rose"><Sparkles /></span><div><h2>DeepSeek API</h2><p>可选直连。测试成功后会成为默认模型；本机 OAuth 代理会保留，随时可以切回。</p></div><StatusDot status={deepseekSettings?.model.status || 'unverified'} /></div>
          <div className="connection-summary">
            <div><span>模型生成</span><strong className={deepseekSettings?.model.status === 'verified' ? 'success-text' : 'warning-text'}>{deepseekSettings?.model.message || '正在读取'}</strong></div>
            <div><span>API Key</span><strong className={deepseekSettings?.has_api_key ? 'success-text' : 'warning-text'}>{deepseekSettings?.has_api_key ? '已保存到本机钥匙串' : '尚未填写'}</strong></div>
            <div><span>当前使用</span><strong className={deepseekSettings?.active ? 'success-text' : ''}>{deepseekSettings?.active ? '已启用' : '未启用'}</strong></div>
            <div><span>最后检测</span><strong>{deepseekSettings?.checked_at ? formatDate(deepseekSettings.checked_at) : '尚未检测'}</strong></div>
          </div>
          <div className="connection-form deepseek-form">
            <label><span>DeepSeek 模型</span><select value={deepseekModel} onChange={event => setDeepseekModel(event.target.value)}>
              <option value="deepseek-v4-flash">DeepSeek V4 Flash · 更快</option>
              <option value="deepseek-v4-pro">DeepSeek V4 Pro · 更强</option>
              <option value="deepseek-v4-flash-vision-exp">DeepSeek V4 Flash Vision · 图片案例（实验）</option>
            </select></label>
            <label className="key-field"><span>API Key</span><input type="password" value={deepseekApiKey} onChange={event => setDeepseekApiKey(event.target.value)} autoComplete="new-password" placeholder={deepseekSettings?.has_api_key ? '已保存到本机钥匙串；留空会保留' : '粘贴 DeepSeek API Key'} /></label>
            <button className="primary-button" onClick={saveDeepseek} disabled={savingDeepseek}>{savingDeepseek ? <LoaderCircle className="spin" /> : <Sparkles />}{savingDeepseek ? '正在测试 DeepSeek' : '测试并启用 DeepSeek'}</button>
            {deepseekSettings?.configured && <button className="secondary-button" onClick={recheckDeepseek} disabled={savingDeepseek}><RefreshCw />重新检测</button>}
          </div>
          <p className="security-note"><ShieldCheck />Key 只存在这台 Mac 的系统钥匙串，不会写入工作台数据库、导出文件或日志。<a href="https://api-docs.deepseek.com/quick_start/pricing/" target="_blank" rel="noreferrer">查看 DeepSeek 模型说明 <ExternalLink /></a></p>
        </section>

        <section className="settings-card provider-card jev-card">
          <div className="settings-title"><span className="settings-icon rose"><Search /></span><div><h2>TypeSafe JEV · 批量内容分类</h2><p>候选内容先由平台真实采集，再由 JEV 按当前人设、业务、内容支柱和风险进行 A/B/C 分流。</p></div><StatusDot status={jevSettings?.verification_status || 'unverified'} /></div>
          <div className="connection-summary">
            <div><span>连接状态</span><strong className={jevSettings?.verification_status === 'verified' ? 'success-text' : 'warning-text'}>{jevSettings?.verification_status === 'verified' ? '已验证' : jevSettings?.last_error_message || '尚未连接'}</strong></div>
            <div><span>API Key</span><strong className={jevSettings?.configured ? 'success-text' : 'warning-text'}>{jevSettings?.configured ? '已保存到本机钥匙串' : '尚未填写'}</strong></div>
            <div><span>分类模型</span><strong>{jevSettings?.model || 'jev-latest'}</strong></div>
            <div><span>最后检测</span><strong>{jevSettings?.last_checked_at ? formatDate(jevSettings.last_checked_at) : '尚未检测'}</strong></div>
          </div>
          <div className="connection-form deepseek-form">
            <label className="key-field"><span>TypeSafe API Key</span><input type="password" value={jevApiKey} onChange={event => setJevApiKey(event.target.value)} autoComplete="new-password" placeholder={jevSettings?.configured ? '已保存到本机钥匙串；留空会保留' : '粘贴 TypeSafe API Key'} /></label>
            <button className="primary-button" onClick={saveJev} disabled={savingJev}>{savingJev ? <LoaderCircle className="spin" /> : <Sparkles />}{savingJev ? '正在验证 JEV' : '测试并保存 JEV'}</button>
            {jevSettings?.configured && <button className="secondary-button" onClick={recheckJev} disabled={savingJev}><RefreshCw />重新检测</button>}
          </div>
          <p className="security-note"><ShieldCheck />本工作台通过 TypeSafe 官方 API 自动调用 JEV；只在官网 Playground 手动试用不会自动接入。请先到<a href="https://console.typesafe.ai/login" target="_blank" rel="noreferrer">官方控制台检查账号权限并获取 API Key <ExternalLink /></a>，再在上方测试并保存。JEV 只接收脱敏后的标题、摘要、平台和公开互动数；不发送原帖链接、作者账号、联系方式、Cookie、登录信息或人设方案原文。<a href="https://docs.typesafe.ai/api" target="_blank" rel="noreferrer">查看官方 API 说明 <ExternalLink /></a></p>
        </section>

        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon green"><Sparkles /></span><div><h2>三个数字员工</h2><p>Skill 已内置进工作流，不需要逐个配置。</p></div></div>
          <div className="skill-grid">
            {skills.map(skill => <SkillCard skill={skill} key={skill.id} />)}
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon amber"><FileText /></span><div><h2>我的 IP 信息</h2><p>选填一次，小红书会自动同步，朋友圈和抖音也会保持同一个人设与语气。</p></div></div>
          <textarea
            className="common-info"
            value={commonInfo}
            onChange={event => setCommonInfo(event.target.value)}
            placeholder={"可以写：\n我的身份、经历和账号定位\n我提供什么产品或服务\n主要客户是谁、他们常说什么\n我习惯怎样说话、哪些词不要用\n不能承诺或不能公开的内容"}
          />
          <div className="settings-footer"><span>{profile?.updated_at ? `上次保存：${formatDate(profile.updated_at)}` : '没有也可以直接创作'}</span><button className="secondary-button" onClick={saveInfo} disabled={savingInfo}><Save />{savingInfo ? '保存中' : '保存 IP 信息'}</button></div>
        </section>
      </div>
    </div>
  )
}

function StatusDot({ status }: { status: string }) {
  const online = status === 'verified'
  return <span className={`large-status ${online ? 'online' : ''}`}>{online ? <><CheckCircle2 />已连接</> : <><AlertTriangle />未验证</>}</span>
}

function SkillCard({ skill }: { skill: SkillStatus }) {
  const icons = { researcher: UserRoundSearch, writer: PenLine, editor: ShieldCheck }
  const labels = { researcher: '研究员', writer: '文案员工', editor: '主编' }
  const Icon = icons[skill.role]
  return (
    <article>
      <span><Icon /></span>
      <div><strong>{labels[skill.role]}</strong><p>{skill.description}</p><small><CheckCircle2 />Skill 已安装 · v{skill.version}</small></div>
    </article>
  )
}

export default App
