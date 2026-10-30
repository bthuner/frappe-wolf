<script setup>
import { Button, PageHeader, PageHeaderTitle, TabButtons } from 'frappe-ui'
import { inject, ref, watch } from 'vue'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { useQueryParam } from '@/composables/useQueryParam'
import { call } from '@/services/api'
import { duration, full } from '@/services/time'

const boot = inject('boot')
const tz = boot.time_zone

const STATUSES = [
  { value: 'unresolved', label: 'Unresolved' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'all', label: 'All' },
]
const status = useQueryParam('status', 'unresolved', STATUSES.map((s) => s.value))

const incidents = ref([])
const cursor = ref(null)
const hasMore = ref(false)
const loading = ref(false)
const error = ref(null)
let ticket = 0

async function load(append = false) {
  const mine = ++ticket
  loading.value = true
  error.value = null
  try {
    const page = await call('frappe_wolf.api.incident_view.list_incidents', {
      status: status.value,
      cursor: append ? cursor.value : null,
    })
    if (mine !== ticket) return // an older filter answered late
    const seen = new Set(append ? incidents.value.map((i) => i.name) : [])
    incidents.value = [...(append ? incidents.value : []), ...page.incidents.filter((i) => !seen.has(i.name))]
    cursor.value = page.paging.next_cursor
    hasMore.value = page.paging.has_more
  } catch (e) {
    if (mine === ticket) error.value = e.message
  } finally {
    if (mine === ticket) loading.value = false
  }
}

function scope(i) {
  return [i.site, i.bench, i.host].filter(Boolean).join(' · ')
}

watch(status, () => load(), { immediate: true })
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Incidents" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="loading" @click="load()" />
  </PageHeader>
  <div class="mx-auto max-w-5xl space-y-3 p-5">
    <TabButtons v-model="status" :options="STATUSES" />
    <p v-if="error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ error }}</p>
    <ul class="wolf-card divide-y divide-[var(--wolf-line)] p-0">
      <li v-for="i in incidents" :key="i.name" class="px-4 py-3">
        <RouterLink :to="{ name: 'incident', params: { incident: i.name } }" class="block">
          <div class="flex flex-wrap items-baseline gap-x-3 text-sm text-ink-gray-6">
            <SeverityGlyph :severity="i.severity" />
            <span class="wolf-badge">{{ i.status }}</span>
            <span>{{ scope(i) }}</span>
            <span class="wolf-data">{{ i.name }}</span>
          </div>
          <div class="text-base text-ink-gray-9">{{ i.title }}</div>
          <div class="text-sm text-ink-gray-6">
            <time class="wolf-data" :datetime="i.started_at">{{ full(i.started_at, tz) }}</time>
            <span v-if="i.resolved_at">, lasted {{ duration(i.started_at, i.resolved_at) }}</span>
            <span v-if="i.assigned_name"> · {{ i.assigned_name }}</span>
            <span v-if="i.merged_into"> · merged into {{ i.merged_into }}</span>
          </div>
        </RouterLink>
      </li>
    </ul>
    <p v-if="!loading && !error && !incidents.length" class="text-sm text-ink-gray-6">
      No {{ status === 'all' ? '' : status }} incidents you can see.
    </p>
    <Button v-if="hasMore" label="Load more" :loading="loading" @click="load(true)" />
  </div>
</template>
