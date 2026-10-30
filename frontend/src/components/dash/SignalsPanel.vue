<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { ago, full } from '@/services/time'

// The newest value of each signal series and its state, as the server judged it
// (wolf/catalog.py). The panel adds no threshold of its own.
const props = defineProps({
  signals: { type: Object, required: true },
  timeZone: { type: String, required: true },
})

const STATE = {
  firing: { label: 'Rule firing', glyph: '●', color: 'var(--wolf-critical)' },
  stale: { label: 'No new data', glyph: '◌', color: 'var(--wolf-warning)' },
  watched: { label: 'Within its rules', glyph: '✓', color: 'var(--wolf-ok)' },
  unwatched: { label: 'No rule watches this', glyph: '–', color: 'var(--wolf-muted)' },
}
const GROUPS = { workers: 'Workers and queues', processes: 'Bench processes', redis: 'Redis', database: 'Database', backups: 'Backups', tls: 'TLS certificates', host: 'Host resources' }

const groups = computed(() => {
  const by = {}
  for (const s of props.signals.series) (by[s.group] ||= []).push(s)
  return Object.keys(GROUPS).filter((g) => by[g]).map((g) => ({ key: g, label: GROUPS[g], series: by[g], counts: props.signals.counts[g] || {} }))
})
const where = (s) => [s.site, s.bench, s.host].filter(Boolean).join(' · ')
const label = (s) => (s.label_key ? `${s.label} (${s.label_key})` : s.label)
const value = (s) => (s.value == null ? '' : `${Number(s.value).toLocaleString('en', { maximumFractionDigits: 2 })} ${s.unit}`)
const rule = (r) => `${r.rule_name}: ${r.aggregation} ${r.operator} ${r.threshold} over ${r.window_minutes} min`
const email = computed(() => Object.entries(props.signals.email || {}))
</script>

<template>
  <section class="wolf-card space-y-4" aria-label="Signals">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-base font-semibold text-ink-gray-9">Signals</h2>
      <span class="text-sm text-ink-gray-6">newest value of each series; thresholds come from the alert rules</span>
    </div>
    <p v-if="signals.truncated" class="text-sm text-ink-gray-6">
      Showing {{ signals.series.length }} of {{ signals.total_series }} series, problems first. The counts per group are exact.
    </p>

    <div v-for="g in groups" :key="g.key" class="space-y-1">
      <h3 class="text-sm font-semibold text-ink-gray-8">
        {{ g.label }}
        <span class="font-normal text-ink-gray-6">
          <template v-for="(n, st) in g.counts" :key="st"> · {{ n }} {{ STATE[st].label.toLowerCase() }}</template>
        </span>
      </h3>
      <ul class="divide-y divide-[var(--wolf-line)]">
        <li v-for="s in g.series" :key="`${s.metric_name}|${s.host}|${s.bench}|${s.site}|${s.label_key}`" class="flex flex-wrap items-baseline gap-x-3 py-1.5 text-sm">
          <span class="inline-flex min-w-[11rem] items-center gap-1" :style="{ color: STATE[s.state].color }">
            <span aria-hidden="true">{{ STATE[s.state].glyph }}</span>{{ STATE[s.state].label }}
          </span>
          <span class="text-ink-gray-9">{{ label(s) }}</span>
          <span v-if="s.value != null && !s.stale" class="wolf-data text-ink-gray-9">{{ value(s) }}</span>
          <span class="text-ink-gray-6">{{ where(s) }}</span>
          <span class="text-ink-gray-6">
            <template v-if="s.state === 'stale' || s.stale">
              {{ s.state === 'firing' ? 'and no new data: ' : '' }}last sample <time :title="full(s.last_at, timeZone)">{{ ago(s.last_at) }} ago</time>
            </template>
          </span>
          <span v-for="r in s.rules" :key="r.name" class="text-ink-gray-6">{{ rule(r) }}</span>
          <RouterLink v-for="i in s.incidents" :key="i" class="wolf-link" :to="{ name: 'incident', params: { incident: i } }">{{ i }}</RouterLink>
          <span v-if="signals.caveats[s.metric_name]" class="w-full text-xs text-ink-gray-6">{{ signals.caveats[s.metric_name] }}</span>
        </li>
      </ul>
    </div>

    <div v-if="email.length" class="space-y-1">
      <h3 class="text-sm font-semibold text-ink-gray-8">Email queue (last 24 h)</h3>
      <p v-for="[category, e] in email" :key="category" class="text-sm text-ink-gray-8">
        {{ category === 'email_queue_backlog' ? 'Backlog reported' : 'Sending errors reported' }}:
        {{ e.records }} event records, last <time :title="full(e.last_at, timeZone)">{{ ago(e.last_at) }} ago</time>
      </p>
    </div>

    <details v-if="signals.missing.length" class="text-sm text-ink-gray-6">
      <summary>{{ signals.missing.length }} signals without data in the last {{ signals.lookback_days }} days</summary>
      <p class="mt-1">
        Not collected for this scope, not set up on the agent, or not available here. They are not zero:
        {{ signals.missing.map((m) => m.label).join(', ') }}.
      </p>
    </details>
  </section>
</template>
