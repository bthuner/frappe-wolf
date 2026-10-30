<script setup>
import { computed } from 'vue'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { clock, full } from '@/services/time'

const props = defineProps({
  row: { type: Object, required: true },
  timeZone: { type: String, required: true },
  expanded: { type: Boolean, default: false },
  anchor: { type: String, required: true },
})
defineEmits(['toggle'])

const ICONS = { event: 'lucide-zap', deployment: 'lucide-rocket', activity: 'lucide-user', delivery: 'lucide-send' }
const RELATION = { linked: 'Evidence', context: 'Context, same scope', preceding_change: 'Preceding change' }

const scope = computed(() => [props.row.scope?.site, props.row.scope?.bench, props.row.scope?.host].filter(Boolean).join(' · '))
const repeated = computed(() => props.row.count > 1)
// what the expanded row shows: recorded fields only, as text
const details = computed(() => {
  const r = props.row
  if (r.kind === 'delivery') {
    return { channel: r.channel, action: r.action, target: r.target, result: r.result, http_status: r.http_status, error: r.error, digest: r.digest }
  }
  if (r.kind === 'activity') return null
  return r.details && Object.keys(r.details).length ? r.details : null
})
const detailText = computed(() => (details.value ? JSON.stringify(details.value, null, 2) : ''))
</script>

<template>
  <li :id="anchor" class="wolf-row" :class="{ 'wolf-row-context': row.family === 'context' }">
    <time class="wolf-data wolf-row-time" :datetime="row.at" :title="full(row.at, timeZone)">{{ clock(row.at, timeZone) }}</time>
    <span class="wolf-row-icon size-4" :class="ICONS[row.kind]" aria-hidden="true" />
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-baseline gap-x-2">
        <SeverityGlyph v-if="row.severity" :severity="row.severity" :show-label="false" />
        <span class="text-ink-gray-9">{{ row.summary }}</span>
        <span v-if="RELATION[row.relation]" class="wolf-badge">{{ RELATION[row.relation] }}</span>
        <span v-if="row.kind === 'delivery'" class="wolf-badge">{{ row.result === 'accepted' ? 'accepted by the endpoint' : 'failed' }}</span>
      </div>
      <div class="text-sm text-ink-gray-6">
        <span v-if="scope">{{ scope }}</span>
        <span v-if="row.kind === 'activity' && row.user_name"> {{ row.user_name }}</span>
        <span v-if="repeated" class="wolf-data">
          · {{ row.count }} reported, last <time :datetime="row.last_seen" :title="full(row.last_seen, timeZone)">{{ clock(row.last_seen, timeZone) }}</time>
        </span>
      </div>
      <p v-if="row.text" class="whitespace-pre-wrap text-ink-gray-8">{{ row.text }}</p>
      <p v-if="row.unavailable" class="text-sm text-ink-gray-6">
        The deployment event was deleted by retention; the incident kept its time.
      </p>
      <button v-if="details" type="button" class="wolf-link text-sm" :aria-expanded="expanded" @click="$emit('toggle')">
        {{ expanded ? 'Hide details' : 'Details' }}
      </button>
      <pre v-if="expanded && details" class="wolf-data wolf-details">{{ detailText }}</pre>
    </div>
    <a class="wolf-row-anchor text-sm" :href="`#${anchor}`" :aria-label="`Link to this row (${clock(row.at, timeZone)})`">#</a>
  </li>
</template>
