<script setup>
import { PageHeader, PageHeaderTitle } from 'frappe-ui'
import { computed, inject, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { call } from '@/services/api'
import { clock, day, full } from '@/services/time'

const route = useRoute()
const boot = inject('boot')
const tz = boot.time_zone
const group = ref(null)
const error = ref(null)

async function load() {
  error.value = null
  try {
    group.value = await call('frappe_wolf.api.explore.get_error_group', { fingerprint: route.params.fingerprint })
  } catch (e) {
    group.value = null
    error.value = e.status === 404 ? 'No error group with this fingerprint that you can see.' : e.message
  }
}
watch(() => route.params.fingerprint, load, { immediate: true })

const payload = computed(() => group.value?.representative.payload || {})
const traceback = computed(() => payload.value.traceback || payload.value.exception || null)
const rest = computed(() => {
  const { traceback: _t, exception: _e, ...others } = payload.value
  return Object.keys(others).length ? JSON.stringify(others, null, 2) : ''
})
const where = (s) => [s.site, s.bench, s.host].filter(Boolean).join(' · ')
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Error group" />
  </PageHeader>
  <section v-if="error" class="max-w-2xl p-5 text-base text-ink-gray-8">
    <p>{{ error }}</p>
    <RouterLink class="wolf-link" :to="{ name: 'errors' }">Back to errors</RouterLink>
  </section>
  <div v-else-if="group" class="mx-auto max-w-6xl space-y-5 p-5">
    <header class="wolf-card space-y-2">
      <SeverityGlyph :severity="group.severity" />
      <h1 class="text-xl font-semibold text-ink-gray-9">{{ group.title }}</h1>
      <dl class="wolf-facts text-sm">
        <dt>Records</dt><dd class="wolf-data">{{ group.records }}</dd>
        <dt>Reported occurrences</dt><dd class="wolf-data">{{ group.occurrences }}</dd>
        <dt>First seen</dt><dd class="wolf-data">{{ full(group.first_seen, tz) }}</dd>
        <dt>Last seen</dt><dd class="wolf-data">{{ full(group.last_seen, tz) }}</dd>
        <dt>Fingerprint</dt><dd class="wolf-data break-all">{{ group.fingerprint }}</dd>
      </dl>
      <p class="text-sm text-ink-gray-6">Every record of this group you may see, whatever its age.</p>
    </header>

    <section class="wolf-card space-y-2" aria-label="Where">
      <h2 class="text-base font-semibold text-ink-gray-9">Where it happened</h2>
      <table class="w-full text-left text-sm">
        <thead class="text-ink-gray-6">
          <tr><th class="py-1 font-normal">Site · bench · host</th><th class="font-normal">Records</th><th class="font-normal">Reported</th><th class="font-normal">First seen</th><th class="font-normal">Last seen</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in group.scopes" :key="where(s)" class="border-t border-[var(--wolf-line)]">
            <td class="py-1">
              <RouterLink v-if="s.site" class="wolf-link" :to="{ name: 'site', params: { site: s.site } }">{{ s.site }}</RouterLink>
              <span class="text-ink-gray-6">{{ s.site ? ' · ' : '' }}{{ [s.bench, s.host].filter(Boolean).join(' · ') }}</span>
            </td>
            <td class="wolf-data">{{ s.records }}</td>
            <td class="wolf-data">{{ s.occurrences }}</td>
            <td class="wolf-data">{{ day(s.first_seen, tz) }} {{ clock(s.first_seen, tz) }}</td>
            <td class="wolf-data">{{ day(s.last_seen, tz) }} {{ clock(s.last_seen, tz) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="group.incidents.length || group.incidents_not_visible" class="wolf-card space-y-1" aria-label="Incidents">
      <h2 class="text-base font-semibold text-ink-gray-9">Incidents with these errors</h2>
      <p v-for="i in group.incidents" :key="i.name" class="text-sm">
        <SeverityGlyph :severity="i.severity" :show-label="false" />
        <RouterLink class="wolf-link" :to="{ name: 'incident', params: { incident: i.name } }">{{ i.name }}</RouterLink>
        {{ i.title }} <span class="text-ink-gray-6">({{ i.status }})</span>
      </p>
      <p v-if="group.incidents_not_visible" class="text-sm text-ink-gray-6">and {{ group.incidents_not_visible }} you may not see</p>
    </section>

    <section class="wolf-card space-y-2" aria-label="Newest record">
      <h2 class="text-base font-semibold text-ink-gray-9">Newest record</h2>
      <p class="text-sm text-ink-gray-6">
        {{ where(group.representative) }} · last seen {{ full(group.representative.last_seen, tz) }}
      </p>
      <pre v-if="traceback" class="wolf-data wolf-details">{{ traceback }}</pre>
      <pre v-if="rest" class="wolf-data wolf-details">{{ rest }}</pre>
      <p v-if="!traceback && !rest" class="text-sm text-ink-gray-6">This record carries no details.</p>
    </section>

    <section class="wolf-card space-y-1" aria-label="Records">
      <h2 class="text-base font-semibold text-ink-gray-9">Records, most recently seen first</h2>
      <ul class="text-sm">
        <li v-for="r in group.recent_records" :key="r.name" class="flex flex-wrap gap-x-3 py-0.5">
          <time class="wolf-data text-ink-gray-6" :title="`last seen ${full(r.last_seen, tz)}`">{{ day(r.last_seen, tz) }} {{ clock(r.last_seen, tz) }}</time>
          <span>{{ where(r) }}</span>
          <span v-if="r.count > 1" class="wolf-data text-ink-gray-6">{{ r.count }} reported</span>
          <RouterLink v-if="r.incident" class="wolf-link" :to="{ name: 'incident', params: { incident: r.incident } }">{{ r.incident }}</RouterLink>
        </li>
      </ul>
      <p v-if="group.more_records" class="text-sm text-ink-gray-6">Older records are not listed; the counts above include them.</p>
    </section>
  </div>
</template>
