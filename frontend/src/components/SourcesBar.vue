<template>
  <div v-if="sources.length" class="sources-bar">
    <div class="sources-label">Sources used</div>
    <div class="source-badges">
      <component
        v-for="(s, i) in sources"
        :key="i"
        :is="s.url ? 'a' : 'span'"
        :href="s.url || undefined"
        target="_blank"
        rel="noopener noreferrer"
        class="source-badge"
      >
        <span v-if="s.ticker">{{ s.ticker }}</span>
        <span v-if="s.ticker && s.form" class="sep">·</span>
        <span v-if="s.form">{{ s.form }}</span>
        <span v-if="s.filed_date" class="sep">·</span>
        <span v-if="s.filed_date">{{ s.filed_date }}</span>
        <span v-if="s.accession" class="acc">{{ formatAccession(s.accession) }}</span>
      </component>
    </div>
  </div>
</template>

<script setup>
defineProps({ sources: { type: Array, default: () => [] } })

function formatAccession(acc) {
  if (!acc) return ''
  const clean = acc.replace(/-/g, '')
  if (clean.length === 18) {
    return `${clean.slice(0,10)}-${clean.slice(10,12)}-${clean.slice(12)}`
  }
  return acc
}
</script>
