<script setup>
import { ref, computed } from 'vue'

const props = defineProps({ call: { type: Object, required: true } })
const open = ref(false)

const statusLabel = computed(() => {
  if (props.call.status === 'running') return '执行中'
  return props.call.is_error ? '失败' : '完成'
})

// 工具结果里挑最值得让用户一眼看到的那句
const highlight = computed(() => {
  const r = props.call.result
  if (!r || typeof r !== 'object') return ''
  switch (props.call.name) {
    case 'query_tracking':
      return `${r.company ?? ''} ${r.tracking_no ?? ''} · ${r.status ?? ''}`
    case 'assess_claim': {
      const c = r.compensation ?? {}
      return `责任方 ${r.responsible_party?.primary ?? '-'} · 建议主张 ${c.suggested_claim ?? '-'} 元`
    }
    case 'check_return_eligibility':
      return `${({ yes: '可以退', no: '不能退', conditional: '有条件' })[r.verdict] ?? r.verdict ?? ''} · ${r.summary ?? ''}`
    case 'compare_shipping':
      return r.recommended ? `推荐 ${r.recommended.short} · ${r.recommended.total} 元 · ${r.recommended.eta}` : ''
    case 'search_knowledge':
      return `命中 ${r.hits?.length ?? 0} 条:${r.hits?.[0]?.来源 ?? ''}`
    case 'create_task':
      return `${r.id ?? ''} ${r.title ?? ''}`
    case 'update_task':
      return `${r.current_step_title ?? ''} · 进度 ${r.progress ?? 0}%`
    case 'schedule_followup':
      return `${r.due_at_text ?? ''} · ${r.message ?? ''}`
    case 'save_material':
      return r.title ?? ''
    case 'get_current_time':
      return r.now ?? ''
    default:
      return ''
  }
})

const pretty = computed(() => JSON.stringify(props.call.result ?? {}, null, 2))
const argsPretty = computed(() => JSON.stringify(props.call.arguments ?? {}, null, 2))
</script>

<template>
  <div class="trace" :class="{ err: call.is_error }">
    <button class="head" @click="open = !open">
      <span class="dot" :class="call.status === 'running' ? 'spin' : call.is_error ? 'bad' : 'ok'" />
      <span class="name">{{ call.display || call.name }}</span>
      <span class="hl">{{ highlight }}</span>
      <span class="status">{{ statusLabel }}</span>
      <span class="chev">{{ open ? '▾' : '▸' }}</span>
    </button>
    <div v-if="open" class="detail">
      <div class="sec">入参</div>
      <pre>{{ argsPretty }}</pre>
      <div class="sec">返回</div>
      <pre>{{ pretty }}</pre>
    </div>
  </div>
</template>

<style scoped>
.trace {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-soft); margin-bottom: 7px; overflow: hidden;
}
.trace.err { border-color: var(--red); }
.head {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 7px 11px; text-align: left; font-size: 12.5px;
}
.head:hover { background: var(--bg-hover); }
.dot { flex: 0 0 7px; width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
.dot.ok { background: var(--green); }
.dot.bad { background: var(--red); }
.dot.spin { background: var(--amber); animation: pulse 1s infinite; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .3 } }
.name { font-weight: 600; color: var(--text); flex: 0 0 auto; }
.hl { color: var(--text-dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status { color: var(--text-faint); font-size: 11px; }
.chev { color: var(--text-faint); font-size: 10px; }
.detail { border-top: 1px solid var(--border); padding: 9px 11px; }
.sec { font-size: 11px; color: var(--text-faint); margin: 5px 0 3px; }
pre {
  margin: 0; background: var(--bg); border: 1px solid var(--border); border-radius: 5px;
  padding: 8px 10px; font-size: 11.5px; max-height: 260px; overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-all;
}
</style>
