<template>
  <div v-if="toolCalls.length" class="trace-panel">
    <button class="trace-toggle" @click="open = !open">
      <span class="arrow" :class="{ open }">▶</span>
      How this was answered · {{ toolCalls.length }} tool call{{ toolCalls.length !== 1 ? 's' : '' }}
    </button>
    <div v-if="open" class="trace-entries">
      <div v-for="(tc, i) in toolCalls" :key="i" class="trace-entry">
        <div class="trace-row-top">
          <span class="tool-name">{{ tc.tool }}</span>
          <span class="trace-stats">{{ tc.rows_returned }} row{{ tc.rows_returned !== 1 ? 's' : '' }} · {{ tc.elapsed_ms }}ms</span>
        </div>
        <div v-if="summary(tc.tool, tc.inputs)" class="trace-inputs">{{ summary(tc.tool, tc.inputs) }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
defineProps({ toolCalls: { type: Array, default: () => [] } })
const open = ref(false)

function summary(tool, inputs) {
  if (!inputs) return ''
  switch (tool) {
    case 'search_documents': {
      const q = inputs.query || ''
      return `"${q.length > 90 ? q.slice(0, 90) + '…' : q}"`
    }
    case 'query_financials': {
      const t = (inputs.tickers || []).join(', ')
      const items = inputs.line_items || []
      const itemStr = items.length ? ': ' + items.join(', ') : ''
      const ps = inputs.period_start ? ` · ${inputs.period_start}` : ''
      const pe = inputs.period_end ? ` → ${inputs.period_end}` : ''
      return `${t}${itemStr}${ps}${pe}`
    }
    case 'get_kpi': {
      const t = (inputs.tickers || []).join(', ')
      const k = inputs.kpis || []
      const kStr = k.length ? ': ' + k.join(', ') : ''
      const ps = inputs.period_start ? ` · ${inputs.period_start}` : ''
      return `${t}${kStr}${ps}`
    }
    case 'get_financial_trends': {
      const m = (inputs.metrics || []).join(', ')
      const p = inputs.periods ? ` · ${inputs.periods}q` : ''
      return `${inputs.ticker || ''}${m ? ': ' + m : ''}${p}`
    }
    case 'get_insider_activity': {
      const since = inputs.since ? ` since ${inputs.since}` : ''
      return `${inputs.ticker || ''}${since}`
    }
    case 'compare_peers': {
      const others = (inputs.tickers || []).filter(t => t !== inputs.focus_ticker).join(', ')
      return `${inputs.focus_ticker || ''} vs ${others} · ${inputs.metric || ''}`
    }
    case 'fetch_latest_filing': {
      const t = (inputs.tickers || []).join(', ')
      const f = (inputs.forms || []).join(', ')
      const since = inputs.since ? ` since ${inputs.since}` : ''
      return `${t}${f ? ' · ' + f : ''}${since}`
    }
    case 'onboard_company':
      return inputs.ticker || ''
    default: {
      const parts = Object.entries(inputs)
        .filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
        .map(([, v]) => String(v))
      return parts.join(' · ')
    }
  }
}
</script>
