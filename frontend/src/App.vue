<template>
  <div id="app">
    <header>
      <div class="header-left">
        <div class="brand">Peer<span>Desk</span></div>
        <div class="tagline">Grounded financial analysis from SEC filings — any public company</div>
      </div>
      <button class="btn-new" @click="newConversation" :disabled="loading">
        + New conversation
      </button>
    </header>

    <div class="shell">
      <!-- Sidebar -->
      <aside class="sidebar">
        <!-- Loaded companies -->
        <div v-if="loadedCompanies.length">
          <div class="sidebar-label">Loaded companies</div>
          <div class="company-chips">
            <button
              v-for="c in loadedCompanies"
              :key="c.ticker"
              class="company-chip"
              :title="c.name || c.ticker"
              @click="prefillQuestion(`Tell me about ${c.ticker}'s recent financial performance`)"
            >{{ c.ticker }}</button>
          </div>
        </div>

        <!-- Conversations -->
        <div v-if="conversations.length">
          <div class="sidebar-label">Conversations</div>
          <div class="history-list">
            <button
              v-for="(conv, i) in conversations"
              :key="i"
              class="history-item"
              :class="{ active: currentConvIdx === i }"
              @click="currentConvIdx = i"
              :title="conv.messages[0]?.question"
            >
              {{ conv.messages[0]?.question }}
            </button>
          </div>
        </div>

        <!-- Export -->
        <div class="export-section">
          <div class="sidebar-label">Export to Excel</div>
          <a class="export-link" href="/export/financials?statement=balance" download>
            Balance Sheet (all tickers)
          </a>
          <a class="export-link" href="/export/financials?statement=income" download>
            Income Statement (all tickers)
          </a>
          <a class="export-link" href="/export/financials?statement=cashflow" download>
            Cash Flow (all tickers)
          </a>
          <a class="export-link" href="/export/financials" download>
            All GAAP statements
          </a>
          <a class="export-link" href="/export/kpis" download>
            KPIs
          </a>
        </div>
      </aside>

      <main class="main" ref="mainEl">
        <!-- Conversation thread -->
        <div v-if="currentConv" class="thread">
          <MessageBubble
            v-for="(msg, i) in currentConv.messages"
            :key="i"
            :msg="msg"
            :is-latest="i === currentConv.messages.length - 1"
            @follow-up="handleQuestion"
          />
        </div>

        <!-- Empty state -->
        <div v-else class="empty-state">
          <div class="empty-title">Ask about any public company</div>
          <div class="demo-grid">
            <button
              v-for="q in DEMO_QUESTIONS"
              :key="q.text"
              class="demo-card"
              :disabled="loading"
              @click="handleQuestion(q.text)"
            >
              <span class="demo-tag">{{ q.tag }}</span>
              <span class="demo-text">{{ q.text }}</span>
            </button>
          </div>
        </div>

        <!-- Live steps while agent is working -->
        <div v-if="loading" class="steps-panel">
          <div
            v-for="(step, i) in steps"
            :key="i"
            class="step-item"
            :class="step.status"
          >
            <span class="step-dot">
              <span v-if="step.status === 'running'" class="step-spinner" />
              <span v-else class="step-check">✓</span>
            </span>
            <span class="step-label">{{ stepLabel(step) }}</span>
            <span v-if="step.status === 'done'" class="step-meta">
              {{ step.rows }} {{ step.rows === 1 ? 'row' : step.tool === 'search_documents' ? 'chunks' : 'rows' }} · {{ (step.elapsed_ms / 1000).toFixed(1) }}s
            </span>
          </div>
          <div v-if="!steps.length" class="step-item">
            <span class="step-dot"><span class="step-spinner" /></span>
            <span class="step-label">Planning…</span>
          </div>
        </div>

        <!-- Input -->
        <QuestionBar ref="questionBarRef" :loading="loading" @submit="handleQuestion" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import QuestionBar from './components/QuestionBar.vue'
import MessageBubble from './components/MessageBubble.vue'

const DEMO_QUESTIONS = [
  { tag: 'Trend',    text: "How has NVIDIA's revenue and gross margin trended over the last 8 quarters?" },
  { tag: 'Peers',   text: "Compare Microsoft and Google's revenue growth over the last four quarters." },
  { tag: 'Balance', text: "What is Salesforce's current debt, cash position, and buyback activity?" },
  { tag: 'Insiders', text: "Are any NVIDIA executives buying or selling shares recently?" },
  { tag: 'Filings', text: "What does Intuit's latest 10-Q say about competition and macro risks?" },
  { tag: 'Quick',   text: "What was Apple's net income and EPS last quarter?" },
]

const loading        = ref(false)
const steps          = ref([])         // [{tool, inputs, status:'running'|'done', rows, elapsed_ms}]
const conversations  = ref([])         // [{messages: [{question,answer,follow_ups,tool_calls,sources}]}]
const currentConvIdx = ref(null)
const mainEl         = ref(null)
const questionBarRef = ref(null)
const loadedCompanies = ref([])

function scrollToBottom() {
  nextTick(() => {
    if (mainEl.value) mainEl.value.scrollTo({ top: mainEl.value.scrollHeight, behavior: 'smooth' })
  })
}

function prefillQuestion(text) {
  questionBarRef.value?.fill(text)
}

onMounted(async () => {
  try {
    const res = await fetch('/companies')
    if (res.ok) loadedCompanies.value = await res.json()
  } catch {}
})

const currentConv = computed(() =>
  currentConvIdx.value !== null ? conversations.value[currentConvIdx.value] : null
)

function newConversation() {
  currentConvIdx.value = null
}

function stepLabel(step) {
  const { tool, inputs } = step
  switch (tool) {
    case 'query_financials': {
      const t = (inputs.tickers || []).join(', ')
      const items = (inputs.line_items || [])
      return `Querying financials — ${t}${items.length ? ': ' + items.join(', ') : ''}`
    }
    case 'get_kpi': {
      const t = (inputs.tickers || []).join(', ')
      const k = (inputs.kpis || [])
      return `Fetching KPIs — ${t}${k.length ? ': ' + k.join(', ') : ''}`
    }
    case 'search_documents': {
      const q = inputs.query || ''
      const preview = q.length > 55 ? q.slice(0, 55) + '…' : q
      return `Searching filings — "${preview}"`
    }
    case 'compare_peers':
      return `Comparing peers — ${inputs.focus_ticker || ''}: ${inputs.metric || ''}`
    case 'fetch_latest_filing': {
      const t = (inputs.tickers || []).join(', ')
      return `Checking EDGAR — ${t}`
    }
    case 'get_financial_trends': {
      const m = (inputs.metrics || []).join(', ')
      return `Analyzing trends — ${inputs.ticker || ''}${m ? ': ' + m : ''}`
    }
    case 'get_insider_activity':
      return `Checking insider activity — ${inputs.ticker || ''}`
    case 'onboard_company': {
      const ticker = inputs.ticker || ''
      if (step.status === 'done') {
        if (step.already_loaded && step.cached)  return `Company data current — ${ticker}`
        if (step.incremental)                    return `Updated with latest filings — ${ticker}`
      }
      return `Loading company data from EDGAR — ${ticker}`
    }
    default:
      return tool
  }
}

async function handleQuestion(question) {
  const history = (currentConv.value?.messages || []).map(m => ({
    question: m.question,
    answer: m.answer,
  }))

  if (currentConvIdx.value === null) {
    conversations.value.unshift({ messages: [] })
    if (conversations.value.length > 10) conversations.value.pop()
    currentConvIdx.value = 0
  }

  loading.value = true
  steps.value = []
  scrollToBottom()

  try {
    const res = await fetch('/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const event = JSON.parse(line.slice(6))
        if (event.type === 'tool_start') {
          steps.value.push({ tool: event.tool, inputs: event.inputs, status: 'running', rows: null, elapsed_ms: null })
        } else if (event.type === 'tool_done') {
          const step = [...steps.value].reverse().find(s => s.tool === event.tool && s.status === 'running')
          if (step) {
            step.rows = event.rows
            step.elapsed_ms = event.elapsed_ms
            step.status = 'done'
            if (event.already_loaded !== undefined) {
              step.already_loaded = event.already_loaded
              step.cached         = event.cached
              step.incremental    = event.incremental
            }
          }
        } else if (event.type === 'done') {
          conversations.value[currentConvIdx.value].messages.push({ question, ...event })
          scrollToBottom()
        } else if (event.type === 'error') {
          throw new Error(event.detail)
        }
      }
    }
  } catch (e) {
    conversations.value[currentConvIdx.value].messages.push({
      question,
      answer: e.message,
      tool_calls: [],
      sources: [],
      is_error: true,
    })
    scrollToBottom()
  } finally {
    loading.value = false
    steps.value = []
  }
}
</script>
