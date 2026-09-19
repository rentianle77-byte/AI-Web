<script setup>
import { computed, ref } from 'vue'
import { marked } from 'marked'
import { api } from '../api'

const props = defineProps({ material: Object })
const emit = defineEmits(['close'])
const copied = ref(false)

const html = computed(() => marked.parse(props.material?.content || ''))

async function copy() {
  try {
    await navigator.clipboard.writeText(props.material.content)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch (e) {
    alert('复制失败,请手动选中内容复制')
  }
}
</script>

<template>
  <div class="mask" @click.self="emit('close')">
    <div class="modal">
      <header>
        <h3>{{ material.title }}</h3>
        <button class="btn btn-sm" @click="emit('close')">关闭</button>
      </header>
      <div class="content md" v-html="html" />
      <footer>
        <button class="btn btn-primary" @click="copy">{{ copied ? '已复制 ✓' : '复制全文' }}</button>
        <a class="btn" :href="api.materialExportUrl(material.id)" download>下载 Markdown</a>
        <span class="hint">复制后可直接发给客服 / 商家</span>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: grid; place-items: center; z-index: 100; padding: 24px; }
.modal {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
  width: min(720px, 100%); max-height: 84vh; display: flex; flex-direction: column;
}
header { display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
header h3 { margin: 0; font-size: 15px; flex: 1; }
.content { padding: 18px; overflow-y: auto; flex: 1; }
footer { display: flex; align-items: center; gap: 9px; padding: 12px 18px; border-top: 1px solid var(--border); }
footer a { text-decoration: none; }
.hint { font-size: 11.5px; color: var(--text-faint); margin-left: auto; }
</style>
