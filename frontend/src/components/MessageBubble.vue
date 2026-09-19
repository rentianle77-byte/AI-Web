<script setup>
import { computed } from 'vue'
import { marked } from 'marked'

const props = defineProps({ message: { type: Object, required: true } })

marked.setOptions({ breaks: true, gfm: true })

const isUser = computed(() => props.message.role === 'user')
const isFollowup = computed(() => !!props.message.meta?.is_followup)
const html = computed(() => {
  const text = props.message.content || ''
  return isUser.value ? escapeHtml(text).replace(/\n/g, '<br>') : marked.parse(text)
})

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}
</script>

<template>
  <div class="row" :class="{ user: isUser }">
    <div class="avatar" :class="isUser ? 'av-user' : isFollowup ? 'av-bell' : 'av-bot'">
      {{ isUser ? '我' : isFollowup ? '🔔' : '管' }}
    </div>
    <div class="body">
      <div v-if="isFollowup" class="followup-label">
        <span class="tag tag-purple">主动跟进</span>
        <span class="time">{{ message.created_at_text }} · 系统到点自动发起,你不用来问</span>
      </div>
      <div class="bubble" :class="{ 'bubble-user': isUser, 'bubble-followup': isFollowup }">
        <div class="md" v-html="html" />
      </div>
      <div v-if="!isFollowup" class="time">{{ message.created_at_text }}</div>
    </div>
  </div>
</template>

<style scoped>
.row { display: flex; gap: 11px; margin-bottom: 20px; align-items: flex-start; }
.row.user { flex-direction: row-reverse; }
.avatar {
  flex: 0 0 30px; width: 30px; height: 30px; border-radius: 9px;
  display: grid; place-items: center; font-size: 12px; font-weight: 600; margin-top: 2px;
}
.av-bot { background: var(--accent); color: #fff; }
.av-user { background: var(--bg-hover); color: var(--text-dim); }
.av-bell { background: rgba(167,139,250,.2); }
.body { min-width: 0; max-width: 78%; }
.row.user .body { display: flex; flex-direction: column; align-items: flex-end; }
.bubble {
  background: var(--bg-card); border: 1px solid var(--border);
  padding: 11px 14px; border-radius: var(--radius); border-top-left-radius: 3px;
  overflow-x: auto;
}
.bubble-user {
  background: var(--accent); border-color: var(--accent); color: #fff;
  border-top-left-radius: var(--radius); border-top-right-radius: 3px;
}
.bubble-followup { border-color: var(--purple); background: rgba(167,139,250,.06); }
.followup-label { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.time { font-size: 11px; color: var(--text-faint); margin-top: 4px; }
</style>
