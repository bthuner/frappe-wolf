<script setup>
import { Button, PageHeader } from 'frappe-ui'
import { inject } from 'vue'

import ExploreFilters from '@/components/ExploreFilters.vue'
import LiveStatus from '@/components/LiveStatus.vue'
import LoadingNote from '@/components/LoadingNote.vue'
import PageTitle from '@/components/PageTitle.vue'
import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { useExplore } from '@/composables/useExplore'
import { useLive } from '@/composables/useLive'
import { full } from '@/services/time'

const boot = inject('boot')
const tz = boot.time_zone
const list = useExplore('frappe_wolf.api.explore.list_error_groups', 'groups', { itemKey: (g) => g.fingerprint })

// new events never reshuffle the list under the reader: a button offers them
const hub = inject('live', null)
const live = useLive(hub, {
  room: () => ({ doctype: 'Wolf Event' }),
  matches: (m) => m.kind === 'events',
  refresh: async (reason) => {
    if (reason !== 'manual') return false
    await list.load()
    return true
  },
  polls: false, // a poll would only reshuffle the list: the reader refreshes
})
</script>

<template>
  <PageHeader>
    <PageTitle title="Errors" />
    <div class="flex items-center gap-2">
      <LiveStatus :live="live" :poll-seconds="hub?.pollSeconds" pending-label="New events recorded" />
      <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="list.loading.value" @click="live.apply()" />
    </div>
  </PageHeader>
  <div class="mx-auto max-w-6xl space-y-3 p-5">
    <ExploreFilters />
    <p class="text-sm text-ink-gray-6">
      Groups with error records active in the window. Their figures cover all their records you may see, whatever
      their age; occurrences add up each record's reported count, so they are not a count for the window.
    </p>
    <p v-if="list.error.value" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ list.error.value }}</p>
    <table v-if="list.items.value.length" class="wolf-card w-full text-left text-sm">
      <thead class="text-ink-gray-6">
        <tr>
          <th class="py-1 font-normal">Error</th>
          <th class="font-normal">Records</th>
          <th class="font-normal">Reported occurrences</th>
          <th class="font-normal">Sites</th>
          <th class="font-normal">First seen</th>
          <th class="font-normal">Last seen</th>
          <th class="font-normal">Incidents</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="g in list.items.value" :key="g.fingerprint" class="border-t border-[var(--wolf-line)] align-top">
          <td class="py-1.5 pr-3">
            <SeverityGlyph :severity="g.severity" :show-label="false" />
            <RouterLink class="text-ink-gray-9 hover:underline" :to="{ name: 'error-group', params: { fingerprint: g.fingerprint } }">{{ g.title }}</RouterLink>
            <div class="text-ink-gray-6">{{ [g.latest.site, g.latest.bench, g.latest.host].filter(Boolean).join(' · ') }}</div>
          </td>
          <td class="wolf-data">{{ g.records }}<span v-if="g.records_in_window !== g.records" class="text-ink-gray-6"> ({{ g.records_in_window }} in window)</span></td>
          <td class="wolf-data">{{ g.occurrences }}</td>
          <td class="wolf-data">{{ g.sites }}<span v-if="g.host_wide" class="text-ink-gray-6"> + host-wide</span></td>
          <td class="wolf-data"><time :datetime="g.first_seen">{{ full(g.first_seen, tz, { zone: false }) }}</time></td>
          <td class="wolf-data"><time :datetime="g.last_seen">{{ full(g.last_seen, tz, { zone: false }) }}</time></td>
          <td class="wolf-data">{{ g.incidents || '' }}</td>
        </tr>
      </tbody>
    </table>
    <LoadingNote v-if="list.loading.value && !list.items.value.length && !list.error.value" what="error groups" />
    <p v-if="!list.loading.value && !list.error.value && !list.items.value.length" class="text-sm text-ink-gray-6">No error groups match.</p>
    <p v-if="list.items.value.length" class="text-sm text-ink-gray-6">Times in {{ tz }}.</p>
    <Button v-if="list.hasMore.value" label="Load more" :loading="list.loading.value" @click="list.load(true)" />
  </div>
</template>
