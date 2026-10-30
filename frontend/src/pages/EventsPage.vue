<script setup>
import { Button, PageHeader, PageHeaderTitle } from 'frappe-ui'
import { inject, ref } from 'vue'

import ExploreFilters from '@/components/ExploreFilters.vue'
import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { useExplore } from '@/composables/useExplore'
import { call } from '@/services/api'
import { EVENT_TYPES, SEVERITIES, pick } from '@/services/filters'
import { clock, day, full } from '@/services/time'

const boot = inject('boot')
const tz = boot.time_zone
const list = useExplore('frappe_wolf.api.explore.list_events', 'events', {
  itemKey: (e) => e.name,
  extraArgs: (q) => ({ event_type: pick(q.type, EVENT_TYPES), severity: pick(q.severity, SEVERITIES) }),
})

// payloads load when a row opens, and show as text
const open = ref({})
async function toggle(name) {
  if (open.value[name]) {
    const { [name]: _, ...rest } = open.value
    open.value = rest
    return
  }
  open.value = { ...open.value, [name]: { loading: true } }
  try {
    const event = await call('frappe_wolf.api.explore.get_event', { name })
    open.value = { ...open.value, [name]: { text: JSON.stringify(event.payload, null, 2) } }
  } catch (e) {
    open.value = { ...open.value, [name]: { error: e.message } }
  }
}
const where = (e) => [e.site, e.bench, e.host].filter(Boolean).join(' · ')
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Events" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="list.loading.value" @click="list.load()" />
  </PageHeader>
  <div class="mx-auto max-w-6xl space-y-3 p-5">
    <ExploreFilters types />
    <p v-if="list.meta.value" class="text-sm text-ink-gray-6">
      Event records active in the window, newest first; times in {{ tz }}. A record's count is every repeat it absorbed,
      inside the window or not.
    </p>
    <p v-if="list.error.value" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ list.error.value }}</p>
    <ul class="wolf-card divide-y divide-[var(--wolf-line)] p-0">
      <li v-for="e in list.items.value" :key="e.name" class="space-y-1 px-4 py-2 text-sm">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <time class="wolf-data text-ink-gray-6" :datetime="e.first_seen" :title="full(e.first_seen, tz)">{{ day(e.first_seen, tz) }} {{ clock(e.first_seen, tz) }}</time>
          <SeverityGlyph :severity="e.severity" />
          <span class="text-ink-gray-9">{{ e.title }}</span>
          <span class="wolf-badge">{{ e.event_type.replace(/_/g, ' ') }}</span>
          <span class="text-ink-gray-6">{{ where(e) }}</span>
        </div>
        <div class="flex flex-wrap gap-x-3 text-ink-gray-6">
          <span v-if="e.count > 1" class="wolf-data">{{ e.count }} reported, last {{ clock(e.last_seen, tz) }}</span>
          <span v-if="e.category">{{ e.category }}</span>
          <RouterLink v-if="e.incident" class="wolf-link" :to="{ name: 'incident', params: { incident: e.incident } }">{{ e.incident }}</RouterLink>
          <RouterLink v-if="e.event_type === 'error' && e.fingerprint" class="wolf-link" :to="{ name: 'error-group', params: { fingerprint: e.fingerprint } }">error group</RouterLink>
          <button type="button" class="wolf-link" :aria-expanded="!!open[e.name]" @click="toggle(e.name)">{{ open[e.name] ? 'Hide details' : 'Details' }}</button>
        </div>
        <template v-if="open[e.name]">
          <p v-if="open[e.name].error" role="alert" style="color: var(--wolf-critical)">{{ open[e.name].error }}</p>
          <pre v-else-if="open[e.name].text" class="wolf-data wolf-details">{{ open[e.name].text }}</pre>
        </template>
      </li>
    </ul>
    <p v-if="!list.loading.value && !list.error.value && !list.items.value.length" class="text-sm text-ink-gray-6">No event records match.</p>
    <Button v-if="list.hasMore.value" label="Load more" :loading="list.loading.value" @click="list.load(true)" />
  </div>
</template>
