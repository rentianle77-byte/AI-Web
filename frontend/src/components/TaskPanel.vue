<script setup>
import { computed } from 'vue'
import { api } from '../api'

const props = defineProps({
  task: Object,
  followups: { type: Array, default: () => [] },
  materials: { type: Array, default: () => [] },
})
const emit = defineEmits(['fire-followup', 'preview-material', 'refresh'])

const STATUS = {
  active: { label: '进行中', cls: 'tag' },
  waiting: { label: '等对方回应', cls: 'tag tag-amber' },
  attention: { label: '需要你处理', cls: 'tag tag-red' },
  done: { label: '已完成', cls: 'tag tag-green' },
  cancelled: { label: '已取消', cls: 'tag tag-grey' },
}
const STEP_ICON = { done: '✓', skipped: '⤼', in_progress: '●', pending: '' }
const FU_STATUS = {
  scheduled: { label: '待触发', cls: 'tag tag-grey' },
  fired: { label: '已触发', cls: 'tag tag-purple' },
  acked: { label: '已完成', cls: 'tag tag-green' },
  cancelled: { label: '已取消', cls: 'tag tag-grey' },
}
const FU_KIND = { check: '查进展', remind: '提醒', escalate: '该升级了' }
const MAT_KIND = { script: '话术', letter: '申请书', checklist: '清单', guide: '指引', complaint: '申诉材料', other: '材料' }

const statusInfo = computed(() => STATUS[props.task?.status] ?? STATUS.active)
const pending = computed(() => props.followups.filter((f) => f.status === 'scheduled'))

async function fire(f) {
  emit('fire-followup', f)
}
async function cancel(f) {
  await api.cancelFollowup(f.id)
  emit('refresh')
}
</script>

<template>
  <aside class="panel">
    <div v-if="!task" class="empty">
      <div class="empty-icon">📋</div>
      <p>还没有任务</p>
      <span>说一句你遇到的情况,我会自动建任务并开始推进</span>
    </div>

    <template v-else>
      <!-- 任务头 -->
      <section class="block">
        <div class="head">
          <span class="tag tag-purple">{{ task.type_label }}</span>
          <span :class="statusInfo.cls">{{ statusInfo.label }}</span>
        </div>
        <h3 class="title">{{ task.title }}</h3>
        <div class="meta">{{ task.id }}</div>
        <div class="bar"><div class="fill" :style="{ width: task.progress + '%' }" /></div>
        <div class="meta">进度 {{ task.progress }}% · 当前:{{ task.current_step_title || '—' }}</div>
        <div class="actions">
          <a class="btn btn-sm" :href="api.taskExportUrl(task.id, 'md')" download>导出卷宗</a>
          <a class="btn btn-sm" :href="api.taskExportUrl(task.id, 'zip')" download>打包全部材料</a>
        </div>
      </section>

      <!-- 步骤 -->
      <section class="block">
        <div class="block-title">办理流程</div>
        <ol class="steps">
          <li v-for="s in task.steps" :key="s.key" :class="s.status">
            <span class="node">{{ STEP_ICON[s.status] }}</span>
            <div class="step-body">
              <div class="step-title">{{ s.title }}</div>
              <div v-if="s.note" class="step-note">{{ s.note }}</div>
              <div v-else-if="s.status === 'in_progress'" class="step-hint">{{ s.hint }}</div>
            </div>
          </li>
        </ol>
      </section>

      <!-- 关键信息 -->
      <section v-if="Object.keys(task.details || {}).length" class="block">
        <div class="block-title">案情要素</div>
        <dl class="details">
          <template v-for="(v, k) in task.details" :key="k">
            <dt>{{ k }}</dt><dd>{{ v }}</dd>
          </template>
        </dl>
      </section>

      <!-- 跟进 -->
      <section class="block">
        <div class="block-title">
          主动跟进
          <span v-if="pending.length" class="tag tag-amber">{{ pending.length }} 条待触发</span>
        </div>
        <p v-if="!followups.length" class="muted">还没有安排跟进</p>
        <div v-for="f in followups" :key="f.id" class="fu">
          <div class="fu-head">
            <span :class="FU_STATUS[f.status].cls">{{ FU_STATUS[f.status].label }}</span>
            <span class="fu-kind">{{ FU_KIND[f.kind] || f.kind }}</span>
            <span class="fu-time">{{ f.due_at_text }}</span>
          </div>
          <div class="fu-msg">{{ f.message }}</div>
          <div v-if="f.status === 'scheduled'" class="fu-actions">
            <button class="btn btn-sm" @click="fire(f)">立即触发</button>
            <button class="btn btn-sm" @click="cancel(f)">取消</button>
          </div>
        </div>
      </section>

      <!-- 材料 -->
      <section class="block">
        <div class="block-title">
          生成的材料
          <span v-if="materials.length" class="tag tag-green">{{ materials.length }} 份</span>
        </div>
        <p v-if="!materials.length" class="muted">还没有生成材料</p>
        <div v-for="m in materials" :key="m.id" class="mat" @click="emit('preview-material', m)">
          <div class="mat-head">
            <span class="tag tag-grey">{{ MAT_KIND[m.kind] || m.kind }}</span>
            <span class="mat-title">{{ m.title }}</span>
          </div>
          <div class="mat-preview">{{ (m.content || '').slice(0, 70) }}…</div>
        </div>
      </section>
    </template>
  </aside>
</template>

<style scoped>
.panel {
  width: 340px; flex: 0 0 340px; border-left: 1px solid var(--border);
  background: var(--bg-soft); overflow-y: auto; padding: 16px;
}

/* 窄屏下面板要占满宽度。这条必须写在组件的 scoped 块里 ——
   scoped 样式会带上属性选择器,优先级高于全局 CSS 的媒体查询,
   写在 main.css 里会被上面那条 340px 压掉。 */
@media (max-width: 1100px) {
  .panel {
    width: 100%;
    flex: 1 1 auto;
    border-left: none;
    padding-bottom: 24px;
  }
}
.empty { text-align: center; padding: 60px 20px; color: var(--text-faint); }
.empty-icon { font-size: 34px; margin-bottom: 10px; }
.empty p { color: var(--text-dim); margin: 6px 0; font-weight: 500; }
.empty span { font-size: 12.5px; line-height: 1.6; display: block; }
.block { margin-bottom: 22px; }
.block-title {
  font-size: 12px; color: var(--text-faint); text-transform: uppercase;
  letter-spacing: .05em; margin-bottom: 9px; display: flex; align-items: center; gap: 7px;
}
.head { display: flex; gap: 6px; margin-bottom: 7px; }
.title { margin: 0 0 3px; font-size: 15px; line-height: 1.45; }
.meta { font-size: 11.5px; color: var(--text-faint); margin: 3px 0; }
.bar { height: 4px; background: var(--bg-hover); border-radius: 3px; margin: 9px 0 5px; overflow: hidden; }
.fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width .35s ease; }
.actions { display: flex; gap: 6px; margin-top: 10px; }
.actions .btn { text-decoration: none; }

.steps { list-style: none; margin: 0; padding: 0; }
.steps li { display: flex; gap: 10px; padding-bottom: 12px; position: relative; }
.steps li::before {
  content: ''; position: absolute; left: 9px; top: 20px; bottom: 0; width: 1px; background: var(--border);
}
.steps li:last-child::before { display: none; }
.node {
  flex: 0 0 19px; width: 19px; height: 19px; border-radius: 50%;
  border: 1.5px solid var(--border); background: var(--bg-soft);
  display: grid; place-items: center; font-size: 10px; color: var(--text-faint);
  position: relative; z-index: 1; margin-top: 1px;
}
.steps li.done .node { background: var(--green); border-color: var(--green); color: #fff; }
.steps li.skipped .node { background: var(--bg-hover); }
.steps li.in_progress .node { border-color: var(--accent); color: var(--accent); }
.step-body { min-width: 0; flex: 1; }
.step-title { font-size: 13px; }
.steps li.pending .step-title { color: var(--text-faint); }
.steps li.in_progress .step-title { color: var(--accent); font-weight: 600; }
.step-note { font-size: 12px; color: var(--text-dim); margin-top: 2px; }
.step-hint { font-size: 11.5px; color: var(--text-faint); margin-top: 2px; }

.details { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 12.5px; }
.details dt { color: var(--text-faint); }
.details dd { margin: 0; color: var(--text); word-break: break-all; }

.fu { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 9px 11px; margin-bottom: 7px; }
.fu-head { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }
.fu-kind { font-size: 11.5px; color: var(--text-dim); }
.fu-time { font-size: 11px; color: var(--text-faint); margin-left: auto; }
.fu-msg { font-size: 12.5px; color: var(--text-dim); line-height: 1.55; }
.fu-actions { display: flex; gap: 5px; margin-top: 7px; }

.mat {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 9px 11px; margin-bottom: 7px; cursor: pointer; transition: all .14s;
}
.mat:hover { background: var(--bg-hover); border-color: var(--accent); }
.mat-head { display: flex; align-items: center; gap: 7px; }
.mat-title { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mat-preview { font-size: 11.5px; color: var(--text-faint); margin-top: 3px; line-height: 1.5; max-height: 34px; overflow: hidden; }
.muted { color: var(--text-faint); font-size: 12.5px; margin: 0; }
</style>
