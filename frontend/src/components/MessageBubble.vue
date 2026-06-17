<template>
  <div class="message-bubble" :class="{ 'is-latest': isLatest, 'is-error': msg.is_error }">
    <div class="msg-question">
      <span class="msg-q-label">Q</span>
      <span class="msg-q-text">{{ msg.question }}</span>
    </div>

    <!-- Error state -->
    <div v-if="msg.is_error" class="msg-error">
      <span class="error-icon">⚠</span>
      <span class="error-text">{{ msg.answer }}</span>
    </div>

    <!-- Normal answer -->
    <div v-else class="msg-answer">
      <div class="answer-actions">
        <button class="btn-copy" @click="copyMarkdown" :class="{ copied }">
          {{ copied ? 'Copied!' : 'Copy' }}
        </button>
      </div>
      <div class="answer-body" v-html="renderedAnswer" />
      <SourcesBar :sources="msg.sources" />
      <TracePanel :tool-calls="msg.tool_calls" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { marked } from 'marked'
import SourcesBar from './SourcesBar.vue'
import TracePanel from './TracePanel.vue'

const props = defineProps({
  msg:      { type: Object,  required: true },
  isLatest: { type: Boolean, default: false },
})

marked.setOptions({ breaks: true })
const renderedAnswer = computed(() => marked.parse(props.msg.answer || ''))

const copied = ref(false)
function copyMarkdown() {
  const sources = props.msg.sources || []
  const sourceLines = sources.length
    ? '\n\n---\n**Sources**\n' + sources.map(s =>
        `- ${[s.ticker, s.form, s.filed_date, s.accession].filter(Boolean).join(' · ')}`
      ).join('\n')
    : ''
  const text = `**Q: ${props.msg.question}**\n\n${props.msg.answer}${sourceLines}`
  navigator.clipboard.writeText(text).then(() => {
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  })
}
</script>
