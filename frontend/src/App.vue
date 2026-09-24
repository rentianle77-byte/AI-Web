<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, connectEvents, streamChat } from './api'
import MessageBubble from './components/MessageBubble.vue'
import ToolTrace from './components/ToolTrace.vue'
import TaskPanel from './components/TaskPanel.vue'
import MaterialModal from './components/MaterialModal.vue'

// ---------------- 状态 ----------------
const conversations = ref([])
const conversationId = ref(null)
const messages = ref([])          // 已落库的消息
const liveText = ref('')          // 正在流式输出的文本
const toolCalls = ref([])         // 本轮工具轨迹
const task = ref(null)
const followups = ref([])
const materials = ref([])
const scenarios = ref([])
const status = ref(null)
const clock = ref(null)

const input = ref('')
// 窄屏下三栏放不下,用底部标签在「对话」和「任务」之间切,侧栏做成抽屉
const mobileView = ref('chat')
const sidebarOpen = ref(false)
const sending = ref(false)
const errorMsg = ref('')
const previewMaterial = ref(null)
const scroller = ref(null)
let eventSource = null
let activeStream = null

const hasConversation = computed(() => !!conversationId.value)
const isMock = computed(() => status.value?.llm?.is_mock)
const clockShifted = computed(() => Math.abs(clock.value?.offset_hours ?? 0) > 0.01)

// ---------------- 生命周期 ----------------
onMounted(async () => {
  await Promise.all([refreshStatus(), loadConversations(), loadScenarios()])
  eventSource = connectEvents(onServerEvent)
})
onUnmounted(() => {
  eventSource?.close()
  activeStream?.abort()
})

async function refreshStatus() {
  try {
    status.value = await api.status()
    clock.value = { now: status.value.clock.now, offset_hours: status.value.clock.offset_hours }
  } catch (e) {
    errorMsg.value = `连不上后端:${e.message}。请确认 uvicorn 已在 8000 端口启动。`
  }
}
async function loadConversations() {
  try { conversations.value = await api.listConversations() } catch (e) { /* 忽略 */ }
}
async function loadScenarios() {
  try { scenarios.value = await api.scenarios() } catch (e) { /* 忽略 */ }
}

// ---------------- 后台主动事件 ----------------
function onServerEvent(ev) {
  if (ev.type === 'clock_advanced') {
    clock.value = { now: ev.now, offset_hours: ev.offset_hours }
    if (ev.followups_fired > 0) setTimeout(() => openConversation(conversationId.value, true), 900)
    return
  }
  if (ev.type === 'reset') { resetView(); loadConversations(); return }
  if (!ev.conversation_id || ev.conversation_id !== conversationId.value) {
    if (ev.type === 'followup_fired') loadConversations()
    return
  }
  // 当前对话收到后台跟进产生的内容
  if (ev.type === 'message_saved' && ev.message) {
    if (!messages.value.some((m) => m.id === ev.message.id) && !ev.message.meta?.hidden) {
      messages.value.push(ev.message)
      scrollToBottom()
    }
  } else if (ev.type === 'task_update') {
    task.value = ev.task
  } else if (ev.type === 'material') {
    if (!materials.value.some((m) => m.id === ev.material.id)) materials.value.push(ev.material)
  } else if (ev.type === 'followup') {
    followups.value = ev.followups
  } else if (ev.type === 'followup_done') {
    openConversation(conversationId.value, true)
  }
}

// ---------------- 对话 ----------------
function resetView() {
  conversationId.value = null
  messages.value = []
  task.value = null
  followups.value = []
  materials.value = []
  liveText.value = ''
  toolCalls.value = []
}

function newConversation() {
  resetView()
  input.value = ''
  sidebarOpen.value = false
  mobileView.value = 'chat'
}

async function openConversation(id, silent = false) {
  if (!id) return
  sidebarOpen.value = false
  if (!silent) mobileView.value = 'chat'
  try {
    const data = await api.getConversation(id)
    conversationId.value = id
    messages.value = data.messages
    task.value = data.task
    followups.value = data.followups
    materials.value = data.materials
    if (!silent) { liveText.value = ''; toolCalls.value = [] }
    scrollToBottom()
  } catch (e) {
    errorMsg.value = e.message
  }
}

async function removeConversation(id) {
  if (!confirm('删除这个对话?任务和材料也会一起删掉。')) return
  await api.deleteConversation(id)
  if (id === conversationId.value) resetView()
  loadConversations()
}

function useScenario(s) {
  input.value = s.message
  send()
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  errorMsg.value = ''
  input.value = ''
  sending.value = true
  liveText.value = ''
  toolCalls.value = []

  // 先把用户消息乐观地画出来
  messages.value.push({ id: `tmp-${Date.now()}`, role: 'user', content: text, meta: {}, created_at_text: '' })
  scrollToBottom()

  activeStream = streamChat({ conversationId: conversationId.value, message: text }, (ev) => {
    switch (ev.type) {
      case 'start':
        conversationId.value = ev.conversation_id
        break
      case 'text':
        liveText.value += ev.text
        scrollToBottom()
        break
      case 'tool_start':
        toolCalls.value.push({ ...ev, status: 'running' })
        scrollToBottom()
        break
      case 'tool_end': {
        const i = toolCalls.value.findIndex((c) => c.id === ev.id)
        const done = { ...ev, status: 'done' }
        if (i >= 0) toolCalls.value[i] = { ...toolCalls.value[i], ...done }
        else toolCalls.value.push(done)
        break
      }
      case 'message_saved':
        if (ev.message.meta?.hidden) break
        messages.value = messages.value.filter((m) => !String(m.id).startsWith('tmp-') || m.content !== ev.message.content)
        if (!messages.value.some((m) => m.id === ev.message.id)) messages.value.push(ev.message)
        if (ev.message.role === 'assistant') liveText.value = ''
        scrollToBottom()
        break
      case 'task_update':
        task.value = ev.task
        break
      case 'material':
        if (!materials.value.some((m) => m.id === ev.material.id)) materials.value.push(ev.material)
        break
      case 'followup':
        followups.value = ev.followups
        break
      case 'error':
        errorMsg.value = ev.message
        break
      case 'done':
        if (ev.task) task.value = ev.task
        if (ev.followups) followups.value = ev.followups
        if (ev.materials) materials.value = ev.materials
        break
    }
  })

  try {
    await activeStream.done
  } catch (e) {
    if (e.name !== 'AbortError') errorMsg.value = `请求失败:${e.message}`
  } finally {
    sending.value = false
    activeStream = null
    liveText.value = ''
    loadConversations()
    scrollToBottom()
  }
}

function stop() {
  activeStream?.abort()
  sending.value = false
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ---------------- 演示控制 ----------------
async function advance(hours) {
  const r = await api.advanceClock(hours)
  clock.value = { now: r.now, offset_hours: r.offset_hours }
  if (r.followups_fired > 0) setTimeout(() => openConversation(conversationId.value, true), 1000)
  refreshStatus()
}
async function resetClock() {
  const r = await api.resetClock()
  clock.value = { now: r.now, offset_hours: r.offset_hours }
}
async function resetAll() {
  if (!confirm('清空所有演示数据(对话、任务、跟进、材料)?知识库不受影响。')) return
  await api.resetAll()
  resetView()
  await Promise.all([loadConversations(), refreshStatus()])
}
async function fireFollowup(f) {
  await api.fireFollowup(f.id)
  setTimeout(() => openConversation(conversationId.value, true), 700)
}
async function refreshTaskPanel() {
  if (conversationId.value) openConversation(conversationId.value, true)
}
</script>

<template>
  <div class="app">
    <!-- 顶栏 -->
    <header class="topbar">
      <button class="hamburger" title="会话列表" @click="sidebarOpen = !sidebarOpen">☰</button>
      <div class="brand">
        <span class="logo">📦</span>
        <div>
          <div class="brand-name">快递管家</div>
          <div class="brand-sub">你说目标,它把事办完</div>
        </div>
      </div>

      <div class="demo-bar">
        <span class="clock" :class="{ shifted: clockShifted }">
          🕐 {{ clock?.now || '—' }}
          <em v-if="clockShifted">已快进 {{ clock.offset_hours }} 小时</em>
        </span>
        <button class="btn btn-sm" @click="advance(24)" title="跳到明天,看 Agent 会不会自己来找你">+1 天</button>
        <button class="btn btn-sm" @click="advance(72)">+3 天</button>
        <button class="btn btn-sm" @click="advance(168)">+7 天</button>
        <button v-if="clockShifted" class="btn btn-sm" @click="resetClock">回到现在</button>
        <button class="btn btn-sm" @click="resetAll">清空数据</button>
      </div>

      <div class="model-chip" :class="{ mock: isMock }" :title="isMock ? '未配置 API Key,当前用规则引擎演示流程' : ''">
        {{ status?.llm?.provider || '…' }} · {{ status?.llm?.model || '' }}
        <span v-if="isMock">(演示模式)</span>
      </div>
    </header>

    <div class="layout" :class="'view-' + mobileView">

      <div v-if="sidebarOpen" class="drawer-mask" @click="sidebarOpen = false" />
      <!-- 左:会话列表 -->
      <aside class="sidebar" :class="{ open: sidebarOpen }">
        <button class="btn btn-primary new-btn" @click="newConversation">+ 新建对话</button>
        <div class="conv-list">
          <div
            v-for="c in conversations" :key="c.id"
            class="conv" :class="{ active: c.id === conversationId }"
            @click="openConversation(c.id)"
          >
            <div class="conv-title">{{ c.title }}</div>
            <div class="conv-meta">
              <span class="tag tag-grey">{{ ({ claim: '索赔', return: '退货', ship: '寄件', general: '咨询', auto: '待识别' })[c.agent_type] }}</span>
              <button class="del" title="删除" @click.stop="removeConversation(c.id)">×</button>
            </div>
          </div>
          <p v-if="!conversations.length" class="muted">还没有对话</p>
        </div>
        <div v-if="status" class="sys">
          <div>知识库 {{ status.knowledge.docs }} 篇 / {{ status.knowledge.chunks }} 块</div>
          <div>待触发跟进 {{ status.scheduler.pending_followups }} 条</div>
          <div>调度器 {{ status.scheduler.running ? '运行中' : '已停止' }}</div>
        </div>
      </aside>

      <!-- 中:对话 -->
      <main class="main">
        <div ref="scroller" class="scroll">
          <!-- 欢迎页 -->
          <div v-if="!hasConversation && !messages.length" class="welcome">
            <h1>你的私人快递管家</h1>
            <p class="lede">
              不是查快递的工具,也不是答疑机器人。<br>
              你说一句「我快递坏了/我要退货/我要寄东西」,它自己查轨迹、定责任、算赔偿、写话术、排跟进,
              到点还会主动来找你。
            </p>
            <div class="scen-grid">
              <button v-for="s in scenarios" :key="s.id" class="scen" @click="useScenario(s)">
                <span class="scen-icon">{{ s.icon }}</span>
                <span class="scen-title">{{ s.title }}</span>
                <span class="scen-desc">{{ s.desc }}</span>
              </button>
            </div>
            <p v-if="isMock" class="mock-note">
              当前是<strong>演示模式</strong>(没检测到大模型 API Key)。流程、状态机、跟进、导出都能完整演示;
              在 <code>backend/.env</code> 里填入 Key 后,对话内容会由真实模型生成。
            </p>
          </div>

          <!-- 消息流 -->
          <MessageBubble v-for="m in messages" :key="m.id" :message="m" />

          <!-- 本轮工具轨迹 -->
          <div v-if="toolCalls.length" class="traces">
            <div class="traces-title">Agent 正在做的事</div>
            <ToolTrace v-for="c in toolCalls" :key="c.id" :call="c" />
          </div>

          <!-- 流式文本 -->
          <div v-if="liveText" class="row">
            <div class="avatar av-bot">管</div>
            <div class="bubble"><div class="md">{{ liveText }}</div><span class="caret" /></div>
          </div>
          <div v-else-if="sending && !toolCalls.length" class="thinking">管家正在思考…</div>
        </div>

        <!-- 错误条 -->
        <div v-if="errorMsg" class="err-bar">
          {{ errorMsg }}
          <button class="btn btn-sm" @click="errorMsg = ''">知道了</button>
        </div>

        <!-- 输入区 -->
        <div class="composer">
          <textarea
            v-model="input" rows="2" :disabled="sending"
            placeholder="说一句你遇到的情况,例如:我买的杯子寄到碎了,780 块,中通,单号 73121234567,没保价"
            @keydown="onKeydown"
          />
          <div class="composer-actions">
            <span class="hint">Enter 发送 · Shift+Enter 换行</span>
            <button v-if="sending" class="btn" @click="stop">停止</button>
            <button class="btn btn-primary" :disabled="sending || !input.trim()" @click="send">发送</button>
          </div>
        </div>
      </main>

      <!-- 右:任务面板 -->
      <TaskPanel
        :task="task" :followups="followups" :materials="materials"
        @fire-followup="fireFollowup"
        @preview-material="previewMaterial = $event"
        @refresh="refreshTaskPanel"
      />
    </div>

    <nav class="mobile-tabs">
      <button :class="{ active: mobileView === 'chat' }" @click="mobileView = 'chat'">
        <span>💬</span> 对话
      </button>
      <button :class="{ active: mobileView === 'task' }" @click="mobileView = 'task'">
        <span>📋</span> 任务
        <em v-if="task">{{ task.progress }}%</em>
        <i v-if="materials.length" class="dot" />
      </button>
    </nav>

    <MaterialModal v-if="previewMaterial" :material="previewMaterial" @close="previewMaterial = null" />
  </div>
</template>

<style scoped>
.app { height: 100%; display: flex; flex-direction: column; }

.topbar {
  display: flex; align-items: center; gap: 18px; padding: 10px 18px;
  border-bottom: 1px solid var(--border); background: var(--bg-soft); flex: 0 0 auto;
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo { font-size: 22px; }
.brand-name { font-weight: 600; font-size: 15px; line-height: 1.2; }
.brand-sub { font-size: 11.5px; color: var(--text-faint); }
.demo-bar { display: flex; align-items: center; gap: 6px; margin-left: auto; }
.clock { font-size: 12.5px; color: var(--text-dim); margin-right: 5px; }
.clock.shifted { color: var(--amber); }
.clock em { font-style: normal; font-size: 11px; margin-left: 5px; opacity: .85; }
.model-chip {
  font-size: 11.5px; color: var(--text-faint); padding: 3px 9px;
  border: 1px solid var(--border); border-radius: 20px;
}
.model-chip.mock { color: var(--amber); border-color: var(--amber); }

.layout { flex: 1; display: flex; min-height: 0; }

.sidebar {
  width: 216px; flex: 0 0 216px; border-right: 1px solid var(--border);
  background: var(--bg-soft); display: flex; flex-direction: column; padding: 12px;
}
.new-btn { width: 100%; justify-content: center; margin-bottom: 12px; }
.conv-list { flex: 1; overflow-y: auto; }
.conv { padding: 8px 10px; border-radius: var(--radius-sm); cursor: pointer; margin-bottom: 3px; }
.conv:hover { background: var(--bg-hover); }
.conv.active { background: var(--accent-soft); }
.conv-title {
  font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 3px;
}
.conv-meta { display: flex; align-items: center; gap: 6px; }
.del { color: var(--text-faint); font-size: 15px; line-height: 1; margin-left: auto; padding: 0 3px; }
.del:hover { color: var(--red); }
.sys {
  border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px;
  font-size: 11px; color: var(--text-faint); line-height: 1.8;
}
.muted { color: var(--text-faint); font-size: 12.5px; text-align: center; margin-top: 20px; }

.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.scroll { flex: 1; overflow-y: auto; padding: 22px 26px; }

.welcome { max-width: 680px; margin: 22px auto 0; text-align: center; }
.welcome h1 { font-size: 26px; margin: 0 0 12px; }
.lede { color: var(--text-dim); line-height: 1.8; margin: 0 0 26px; font-size: 14px; }
.scen-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; text-align: left; }
.scen {
  display: flex; flex-direction: column; gap: 4px; padding: 13px 14px;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-card);
  transition: all .14s;
}
.scen:hover { border-color: var(--accent); background: var(--bg-hover); transform: translateY(-1px); }
.scen-icon { font-size: 20px; }
.scen-title { font-weight: 600; font-size: 13.5px; }
.scen-desc { font-size: 12px; color: var(--text-faint); line-height: 1.55; }
.mock-note {
  margin-top: 24px; padding: 11px 14px; border: 1px dashed var(--amber); border-radius: var(--radius);
  font-size: 12.5px; color: var(--text-dim); text-align: left; line-height: 1.7;
}
.mock-note code { background: var(--bg-hover); padding: 1px 5px; border-radius: 4px; }

.traces { margin: 0 0 18px 41px; }
.traces-title { font-size: 11px; color: var(--text-faint); margin-bottom: 6px; letter-spacing: .04em; }

.row { display: flex; gap: 11px; margin-bottom: 20px; align-items: flex-start; }
.avatar {
  flex: 0 0 30px; width: 30px; height: 30px; border-radius: 9px;
  display: grid; place-items: center; font-size: 12px; font-weight: 600; margin-top: 2px;
}
.av-bot { background: var(--accent); color: #fff; }
.bubble {
  background: var(--bg-card); border: 1px solid var(--border); max-width: 78%;
  padding: 11px 14px; border-radius: var(--radius); border-top-left-radius: 3px;
  white-space: pre-wrap;
}
.caret {
  display: inline-block; width: 7px; height: 14px; background: var(--accent);
  vertical-align: text-bottom; margin-left: 2px; animation: blink 1s steps(2) infinite;
}
@keyframes blink { 0%,50% { opacity: 1 } 51%,100% { opacity: 0 } }
.thinking { color: var(--text-faint); font-size: 13px; margin-left: 41px; }

.err-bar {
  display: flex; align-items: center; gap: 10px; margin: 0 26px 8px;
  padding: 9px 13px; border: 1px solid var(--red); border-radius: var(--radius-sm);
  background: rgba(243,109,109,.08); color: var(--red); font-size: 12.5px;
}
.err-bar .btn { margin-left: auto; }

.composer { border-top: 1px solid var(--border); padding: 12px 26px 16px; background: var(--bg-soft); }
.composer textarea { resize: none; line-height: 1.6; }
.composer-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.hint { font-size: 11.5px; color: var(--text-faint); margin-right: auto; }

/* ---------- 窄屏 ----------
   这些规则必须写在组件的 scoped 块里:scoped 会给选择器加属性限定,
   优先级高于 main.css 的全局媒体查询,写在外面会被上面的基础样式压掉。 */
@media (max-width: 1100px) {
  /* 纵向排布,让「任务」视图里面板在上、输入框在下 */
  .layout { flex-direction: column; }

  /* 会话列表 → 左侧滑出的抽屉 */
  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 60;
    width: 78vw;
    max-width: 300px;
    flex: none;
    transform: translateX(-102%);
    transition: transform .22s ease;
    box-shadow: 2px 0 18px rgba(0, 0, 0, .3);
  }
  .sidebar.open { transform: translateX(0); }

  .main { width: 100%; }

  /* 切到「任务」时只收起消息流,输入框保留 ——
     把整个 .main 隐藏会连输入框、停止按钮和错误提示一起藏掉 */
  .layout.view-chat .panel { display: none; }
  .layout.view-task .scroll { display: none; }
  .layout.view-task .main { flex: 0 0 auto; order: 2; }
  .layout.view-task .panel { order: 1; }

  .scroll { padding: 16px; }
  .composer { padding: 10px 16px 12px; }
  .welcome h1 { font-size: 21px; }
  .lede { font-size: 13px; }
  .scen-grid { grid-template-columns: 1fr; }
  .traces { margin-left: 0; }
  .bubble { max-width: 86%; }

  /* 顶栏会挤成一团,让它能横向滚动;品牌名不加 nowrap 会被压成一列竖字 */
  .topbar { gap: 10px; padding: 8px 12px; overflow-x: auto; }
  .brand { flex: 0 0 auto; }
  .brand-name { white-space: nowrap; }
  .brand-sub { display: none; }
  .demo-bar, .model-chip { flex: 0 0 auto; }
}

@media (max-width: 560px) {
  .brand-name { font-size: 14px; }
  .logo { font-size: 18px; }
  .demo-bar .btn { padding: 4px 8px; font-size: 12px; }
  .clock { font-size: 11.5px; }
  .scroll { padding: 12px; }
  .avatar { flex-basis: 26px; width: 26px; height: 26px; }
  .bubble { max-width: 92%; }
}
</style>
