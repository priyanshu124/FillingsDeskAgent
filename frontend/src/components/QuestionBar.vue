<template>
  <div class="question-bar">
    <textarea
      ref="input"
      v-model="text"
      :placeholder="placeholder"
      :disabled="loading"
      rows="1"
      @keydown.enter.exact.prevent="submit"
      @input="autoResize"
    />
    <button class="btn-ask" :disabled="loading || !text.trim()" @click="submit">
      {{ loading ? 'Thinking…' : 'Ask' }}
    </button>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'

const props = defineProps({
  loading: Boolean,
  placeholder: { type: String, default: 'Ask about any public company\'s financials, filings, or insiders…' },
})
const emit = defineEmits(['submit'])

const text = ref('')
const input = ref(null)

function autoResize() {
  const el = input.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

function submit() {
  const q = text.value.trim()
  if (!q || props.loading) return
  emit('submit', q)
  text.value = ''
  nextTick(() => {
    if (input.value) { input.value.style.height = 'auto' }
  })
}

defineExpose({
  fill(q) {
    text.value = q
    nextTick(() => { if (input.value) { input.value.focus(); autoResize() } })
  }
})
</script>
