<script setup>
import { Button, PageHeader, PageHeaderTitle } from 'frappe-ui'
import { inject } from 'vue'

import ExploreFilters from '@/components/ExploreFilters.vue'
import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { useExplore } from '@/composables/useExplore'
import { full } from '@/services/time'

const boot = inject('boot')
const tz = boot.time_zone
const list = useExplore('frappe_wolf.api.explore.list_error_groups', 'groups', { itemKey: (g) => g.fingerprint })
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Errors" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="list.loading.value" @click="list.load()" />
  </PageHeader>
  <div class="mx-auto max-w-6xl space-y-3 p-5">
    <ExploreFilters />
    <p class="text-sm text-ink-gray-6">
      Error records grouped by fingerprint, for groups active in the window. Occurrences add up each record's
      reported count, including repeats outside the window; they are not a count for the window.
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
          <td class="wolf-data">{{ g.records }}</td>
          <td class="wolf-data">{{ g.occurrences }}</td>
          <td class="wolf-data">{{ g.sites }}<span v-if="g.host_wide" class="text-ink-gray-6"> + host-wide</span></td>
          <td class="wolf-data"><time :datetime="g.first_seen">{{ full(g.first_seen, tz).replace(` (${tz})`, '') }}</time></td>
          <td class="wolf-data"><time :datetime="g.last_seen">{{ full(g.last_seen, tz).replace(` (${tz})`, '') }}</time></td>
          <td class="wolf-data">{{ g.incidents || '' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!list.loading.value && !list.error.value && !list.items.value.length" class="text-sm text-ink-gray-6">No error groups match.</p>
    <p v-if="list.items.value.length" class="text-sm text-ink-gray-6">Times in {{ tz }}.</p>
    <Button v-if="list.hasMore.value" label="Load more" :loading="list.loading.value" @click="list.load(true)" />
  </div>
</template>
