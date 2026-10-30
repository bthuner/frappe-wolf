<script setup>
import { Button, PageHeader, PageHeaderTitle, TabButtons } from 'frappe-ui'
import { computed, inject, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import CountTiles from '@/components/dash/CountTiles.vue'
import HttpChart from '@/components/dash/HttpChart.vue'
import MonitoringPanel from '@/components/dash/MonitoringPanel.vue'
import SignalsPanel from '@/components/dash/SignalsPanel.vue'
import { call } from '@/services/api'
import { clock, full } from '@/services/time'

// The overview (no scope) and the site, host and bench dashboards (scope from the route).
const route = useRoute()
const router = useRouter()
const boot = inject('boot')
const tz = boot.time_zone

const RANGES = ['1h', '6h', '24h', '7d'].map((r) => ({ value: r, label: r }))
const range = computed({
  get: () => (RANGES.some((r) => r.value === route.query.range) ? route.query.range : '24h'),
  set: (value) => router.replace({ query: { ...route.query, range: value === '24h' ? undefined : value } }),
})
const scope = computed(() => {
  const { site, host, bench } = route.params
  return Object.fromEntries(Object.entries({ site, host, bench }).filter(([, v]) => v))
})
const title = computed(() => {
  const s = scope.value
  if (s.bench) return `Bench ${s.bench} on ${s.host}`
  if (s.host) return `Host ${s.host}`
  if (s.site) return `Site ${s.site}`
  return 'Overview'
})
const isOverview = computed(() => !Object.keys(scope.value).length)
// a satellite keeps no incidents: saying "none open" would suggest all is well
const hasIncidents = computed(() => boot.capabilities?.incidents?.available)

const data = ref({})
const errors = ref({})
const loading = ref(false)
let ticket = 0

async function load() {
  const mine = ++ticket
  loading.value = true
  const args = { ...scope.value, range: range.value }
  const jobs = {
    counts: call('frappe_wolf.api.scope.get_counts', args),
    http: call('frappe_wolf.api.dashboard.get_http', args),
    signals: call('frappe_wolf.api.dashboard.get_signals', scope.value),
  }
  if (hasIncidents.value) {
    jobs.incidents = call('frappe_wolf.api.incident_view.list_incidents', { ...scope.value, status: 'unresolved', limit: 10 })
  }
  if (isOverview.value || scope.value.host) jobs.inventory = call('frappe_wolf.api.scope.get_inventory', {})
  if (isOverview.value) jobs.monitoring = call('frappe_wolf.api.dashboard.get_monitoring', {})
  const settled = await Promise.allSettled(Object.values(jobs))
  if (mine !== ticket) return // a newer range or scope is loading
  const next = {}
  const failed = {}
  Object.keys(jobs).forEach((k, i) => {
    if (settled[i].status === 'fulfilled') next[k] = settled[i].value
    else failed[k] = settled[i].reason.message // each panel says it failed; the others still show
  })
  data.value = next
  errors.value = failed
  loading.value = false
}

// the inventory part for this page: every host on the overview, this host's benches on a host page
const hosts = computed(() => {
  const all = data.value.inventory?.hosts || []
  return scope.value.host ? all.filter((h) => h.host === scope.value.host) : all
})
const SOURCES = { agent: 'agent', satellite: 'satellite', events: 'events', metrics: 'metrics', incidents: 'incidents' }

watch([scope, range], load, { immediate: true })
</script>

<template>
  <PageHeader>
    <PageHeaderTitle :title="title" />
    <div class="flex items-center gap-2">
      <TabButtons v-model="range" :options="RANGES" />
      <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="loading" @click="load" />
    </div>
  </PageHeader>

  <div class="mx-auto max-w-6xl space-y-5 p-5">
    <p v-if="data.counts?.restricted" class="text-sm text-ink-gray-6">You see the sites, benches and hosts you were granted.</p>

    <CountTiles v-if="data.counts" :counts="data.counts" />
    <p v-else-if="errors.counts" role="alert" class="text-sm" style="color: var(--wolf-critical)">Counts: {{ errors.counts }}</p>

    <section v-if="hasIncidents" class="wolf-card space-y-2" aria-label="Unresolved incidents">
      <div class="flex items-baseline justify-between">
        <h2 class="text-base font-semibold text-ink-gray-9">Unresolved incidents</h2>
        <RouterLink class="wolf-link text-sm" :to="{ name: 'incidents' }">All incidents</RouterLink>
      </div>
      <p v-if="errors.incidents" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ errors.incidents }}</p>
      <ul v-else-if="data.incidents?.incidents.length" class="divide-y divide-[var(--wolf-line)] text-sm">
        <li v-for="i in data.incidents.incidents" :key="i.name" class="flex flex-wrap items-baseline gap-x-3 py-1.5">
          <SeverityGlyph :severity="i.severity" />
          <RouterLink class="text-ink-gray-9 hover:underline" :to="{ name: 'incident', params: { incident: i.name } }">{{ i.title }}</RouterLink>
          <span class="text-ink-gray-6">{{ [i.site, i.bench, i.host].filter(Boolean).join(' · ') }}</span>
          <span class="wolf-data text-ink-gray-6" :title="full(i.started_at, tz)">since {{ clock(i.started_at, tz) }}</span>
          <span class="text-ink-gray-6">{{ i.status }}</span>
        </li>
      </ul>
      <p v-else-if="data.incidents" class="text-sm text-ink-gray-6">None open that you can see.</p>
      <p v-if="data.incidents?.paging.has_more && data.counts" class="text-sm text-ink-gray-6">
        The 10 newest of {{ data.counts.incidents.unresolved }}.
      </p>
    </section>

    <HttpChart v-if="data.http" :http="data.http" :time-zone="tz" />
    <p v-else-if="errors.http" role="alert" class="text-sm" style="color: var(--wolf-critical)">HTTP: {{ errors.http }}</p>

    <section v-if="data.http?.deployments.length" class="wolf-card space-y-1" aria-label="Recent changes">
      <h2 class="text-base font-semibold text-ink-gray-9">Deployments in this window</h2>
      <ul class="text-sm">
        <li v-for="d in data.http.deployments" :key="d.name" class="flex flex-wrap gap-x-3 py-0.5">
          <time class="wolf-data text-ink-gray-6" :title="full(d.at, tz)">{{ clock(d.at, tz) }}</time>
          <span class="text-ink-gray-9">{{ d.title }}</span>
          <span class="text-ink-gray-6">{{ [d.site, d.bench, d.host].filter(Boolean).join(' · ') }}</span>
        </li>
      </ul>
    </section>

    <SignalsPanel v-if="data.signals" :signals="data.signals" :time-zone="tz" />
    <p v-else-if="errors.signals" role="alert" class="text-sm" style="color: var(--wolf-critical)">Signals: {{ errors.signals }}</p>

    <section v-if="hosts.length" class="wolf-card space-y-2" aria-label="Hosts, benches and sites">
      <h2 class="text-base font-semibold text-ink-gray-9">{{ scope.host ? 'Benches and sites on this host' : 'Hosts, benches and sites' }}</h2>
      <ul class="space-y-2 text-sm">
        <li v-for="h in hosts" :key="h.host">
          <RouterLink class="wolf-link" :to="{ name: 'host', params: { host: h.host } }">{{ h.host }}</RouterLink>
          <span class="text-ink-gray-6"> · known from {{ h.sources.map((s) => SOURCES[s]).join(', ') }}</span>
          <ul class="ml-4">
            <li v-for="b in h.benches" :key="b.bench">
              <RouterLink class="wolf-link" :to="{ name: 'bench', params: { host: h.host, bench: b.bench } }">{{ b.bench }}</RouterLink>:
              <template v-for="(s, i) in b.sites" :key="s.site">
                <span v-if="i">, </span>
                <RouterLink class="wolf-link" :to="{ name: 'site', params: { site: s.site } }">{{ s.site }}</RouterLink>
              </template>
            </li>
          </ul>
        </li>
      </ul>
      <p class="text-sm text-ink-gray-6">{{ data.inventory.note }}</p>
    </section>

    <MonitoringPanel v-if="data.monitoring" :monitoring="data.monitoring" :time-zone="tz" compact />
  </div>
</template>
