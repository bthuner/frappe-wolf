<script setup>
import { Button, PageHeader, PageHeaderTitle, Select, TabButtons } from 'frappe-ui'
import { LineChart } from 'frappe-ui/charts'
import { computed, inject, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ScopeInputs from '@/components/ScopeInputs.vue'
import { useQueryParam } from '@/composables/useQueryParam'
import { call } from '@/services/api'
import { RANGES } from '@/services/filters'
import { ago, axisLabel, full } from '@/services/time'

// One metric over a window, as the server's catalog allows it to be read. Every choice is in
// the URL; a selected range of points becomes an explicit since/until.
const route = useRoute()
const router = useRouter()
const boot = inject('boot')
const tz = boot.time_zone

const catalog = ref([])
const labels = ref([])
const result = ref(null)
const error = ref(null)
const loading = ref(false)
const showTable = ref(false)
const pending = ref(null) // first point of a range being selected
let ticket = 0
let labelTicket = 0

const metric = useQueryParam('metric', 'cpu_usage')
const range = useQueryParam('range', '24h', RANGES)
const site = useQueryParam('site')
const host = useQueryParam('host')
const bench = useQueryParam('bench')
const label = useQueryParam('label')
// a metric the catalog does not list (not seen lately): read as a custom level, and said so,
// rather than leaving an empty page
const listed = computed(() => catalog.value.find((m) => m.metric === metric.value))
const spec = computed(() =>
  listed.value || (metric.value && catalog.value.length
    ? { metric: metric.value, label: metric.value, unit: '', kind: 'unknown', seen: false, label_key: 'label', unlisted: true,
        aggregations: ['avg', 'min', 'max', 'last'], default_aggregation: 'avg', group_bys: ['none', 'host', 'bench', 'site', 'host_label'] }
    : null),
)
const aggregation = useQueryParam('agg', '')
const groupBy = useQueryParam('group', '')
const explicit = computed(() => (route.query.since ? { since: route.query.since, until: route.query.until } : null))

const GROUP_LABELS = { none: 'All together', host: 'Per host', bench: 'Per bench', site: 'Per site', host_label: 'Per host and label' }
const metricOptions = computed(() =>
  catalog.value.map((m) => ({
    value: m.metric,
    label: `${m.label}${m.unit ? ` (${m.unit})` : ''}${m.seen ? '' : ' · no data'}`,
  })),
)
const aggOptions = computed(() => (spec.value?.aggregations || []).map((a) => ({ value: a, label: a })))
const groupOptions = computed(() => (spec.value?.group_bys || []).map((g) => ({ value: g, label: GROUP_LABELS[g] })))
const labelOptions = computed(() => [{ value: '', label: `Any ${spec.value?.label_key || 'label'}` }, ...labels.value.map((l) => ({ value: l, label: l }))])

async function loadCatalog() {
  try {
    catalog.value = (await call('frappe_wolf.api.metrics.get_metric_catalog')).metrics
  } catch (e) {
    error.value = e.message
  }
}

async function loadLabels() {
  const mine = ++labelTicket // a slower answer for the previous metric must not land here
  labels.value = []
  if (!spec.value?.label_key) return
  try {
    const answer = await call('frappe_wolf.api.metrics.get_labels', { metric: metric.value, site: site.value, host: host.value, bench: host.value ? bench.value : undefined })
    if (mine === labelTicket) labels.value = answer.labels
  } catch {
    // the filter stays usable with "any"
  }
}

async function query() {
  if (!spec.value) return
  const mine = ++ticket
  loading.value = true
  error.value = null
  const args = {
    metric: metric.value,
    // only what the catalog allows for this metric; otherwise its default
    aggregation: spec.value.aggregations.includes(aggregation.value) ? aggregation.value : spec.value.default_aggregation,
    group_by: spec.value.group_bys.includes(groupBy.value) ? groupBy.value : 'none',
    label: label.value || undefined,
    site: site.value || undefined,
    host: host.value || undefined,
    bench: host.value ? bench.value || undefined : undefined,
    ...(explicit.value || { range: range.value }),
  }
  try {
    const answer = await call('frappe_wolf.api.metrics.query_metric', args)
    if (mine === ticket) result.value = answer
  } catch (e) {
    if (mine === ticket) error.value = e.message
  } finally {
    if (mine === ticket) loading.value = false
  }
}

// the chart: one row per bucket, one column per group; null stays a gap
const long = computed(() => {
  const pts = result.value?.series[0]?.points || []
  return new Set(pts.map(([at]) => axisLabel(at, tz, false))).size < pts.length
})
const rows = computed(() => {
  const series = result.value?.series || []
  if (!series.length) return []
  return series[0].points.map(([at], i) => ({
    at: axisLabel(at, tz, long.value),
    iso: at,
    ...Object.fromEntries(series.map((s) => [s.name, s.points[i][1]])),
  }))
})
const unit = computed(() => result.value?.metric.unit || '')

function onSelect(event) {
  const iso = event?.row?.iso
  if (!iso) return
  if (!pending.value) {
    pending.value = iso
    return
  }
  const [a, b] = [pending.value, iso].sort()
  pending.value = null
  const end = new Date(new Date(b).getTime() + result.value.bucket_seconds * 1000).toISOString()
  router.replace({ query: { ...route.query, since: a, until: end, range: undefined } })
}
function resetRange() {
  pending.value = null
  router.replace({ query: { ...route.query, since: undefined, until: undefined } })
}
function chooseRange(value) {
  pending.value = null
  router.replace({ query: { ...route.query, range: value === '24h' ? undefined : value, since: undefined, until: undefined } })
}
const value = (v) => (v == null ? '' : `${Number(v).toLocaleString('en', { maximumFractionDigits: 2 })} ${unit.value}`)

onMounted(async () => {
  await loadCatalog()
  await Promise.all([loadLabels(), query()])
})
// another metric: its label, aggregation and grouping choices go in the same navigation, so
// no query runs with the previous metric's label
function chooseMetric(value) {
  if (value === metric.value) return
  router.replace({ query: { ...route.query, metric: value, label: undefined, agg: undefined, group: undefined } })
}
watch(() => route.query, query)
watch([metric, site, host, bench], loadLabels)
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Metrics" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="loading" @click="query" />
  </PageHeader>
  <div class="mx-auto max-w-6xl space-y-4 p-5">
    <div class="flex flex-wrap items-center gap-2">
      <Select :model-value="metric" :options="metricOptions" class="w-72" aria-label="Metric" @update:model-value="chooseMetric" />
      <Select v-if="aggOptions.length" :model-value="spec && spec.aggregations.includes(aggregation) ? aggregation : spec?.default_aggregation"
        :options="aggOptions" class="w-28" aria-label="Aggregation" @update:model-value="aggregation = $event" />
      <Select v-if="groupOptions.length" :model-value="spec && spec.group_bys.includes(groupBy) ? groupBy : 'none'"
        :options="groupOptions" class="w-48" aria-label="Group by" @update:model-value="groupBy = $event === 'none' ? '' : $event" />
      <Select v-if="spec?.label_key" :model-value="label" :options="labelOptions" class="w-48" :aria-label="`Filter by ${spec.label_key}`"
        @update:model-value="label = $event" />
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <TabButtons :model-value="explicit ? null : range" :options="RANGES.map((r) => ({ value: r, label: r }))" @update:model-value="chooseRange" />
      <ScopeInputs />
      <Button v-if="explicit" label="Back to a relative window" @click="resetRange" />
    </div>

    <p v-if="spec?.unlisted" class="text-sm" style="color: var(--wolf-warning)">
      {{ metric }} is not in the catalog and had no data you can see in the last 7 days. It is read as a custom metric.
    </p>
    <p v-if="spec" class="text-sm text-ink-gray-6">
      {{ spec.label }}: {{ spec.kind === 'interval_count' ? 'counted per push interval, summed per bucket for a total' : spec.kind === 'gauge' ? 'a level at each sample' : 'a custom metric, read as a level' }}<template v-if="spec.unit">, in {{ spec.unit }}</template>.
      <template v-if="spec.caveat"> {{ spec.caveat }}</template>
    </p>
    <p v-if="error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ error }}</p>

    <section v-if="result" class="wolf-card space-y-2" aria-label="Chart">
      <p class="text-sm text-ink-gray-6">
        {{ result.aggregation }} per {{ result.bucket_seconds >= 3600 ? `${result.bucket_seconds / 3600} h` : `${result.bucket_seconds / 60} min` }},
        read from {{ result.resolution }} data, {{ full(result.window.since, tz) }} to {{ full(result.window.until, tz, { zone: false }) }}.
        Whole buckets only: the one still filling shows when it closes.
        <template v-if="result.retention.clipped"> Data before {{ full(result.retention.kept_from, tz, { zone: false }) }} is no longer kept at this resolution: those buckets are empty, not zero.</template>
        <template v-if="result.groups.total > result.groups.shown"> Showing {{ result.groups.shown }} of {{ result.groups.total }} groups, {{ result.groups.ranked_by }}.</template>
      </p>
      <p v-if="pending" class="text-sm" style="color: var(--wolf-brand-text)">
        Range starts at {{ full(pending, tz, { zone: false }) }}: select the point where it ends.
      </p>
      <div v-if="rows.length" class="h-72">
        <!-- drawn points: a click selects one, as Enter does on the keyboard cursor -->
        <LineChart :data="rows" x="at" :y="result.series.map((s) => s.name)" :x-axis="{ type: 'category' }" show-data-points @select="onSelect" />
      </div>
      <p v-else class="text-sm text-ink-gray-6">No samples of this metric for this scope and window. That is no data, not zero.</p>
      <p v-if="rows.length" class="text-sm text-ink-gray-6">
        Click two points (or move along the chart with the arrow keys and press Enter twice) to look at the range between them.
        Breaks in a line are buckets without samples.
      </p>

      <ul v-if="result.series.length" class="divide-y divide-[var(--wolf-line)] text-sm">
        <li v-for="s in result.series" :key="s.name" class="flex flex-wrap gap-x-3 py-1">
          <span class="text-ink-gray-9">{{ s.name }}</span>
          <span class="wolf-data">{{ result.aggregation }} {{ value(s.value) }}</span>
          <span class="text-ink-gray-6">{{ s.samples }} samples</span>
          <span v-if="result.live" :style="s.stale ? { color: 'var(--wolf-warning)' } : {}" class="text-ink-gray-6">
            {{ s.stale ? 'no new data, ' : '' }}last sample <time :title="full(s.last_sample, tz)">{{ ago(s.last_sample) }} ago</time>
          </span>
          <span v-else class="text-ink-gray-6">last sample in this window at <time>{{ full(s.last_sample, tz, { zone: false }) }}</time></span>
        </li>
      </ul>

      <Button v-if="rows.length" :label="showTable ? 'Hide the data table' : 'Show the data as a table'" @click="showTable = !showTable" />
      <div v-if="showTable" class="max-h-96 overflow-auto">
        <table class="w-full text-left text-sm">
          <caption class="sr-only">{{ spec?.label }}, {{ result.aggregation }} per bucket</caption>
          <thead class="text-ink-gray-6">
            <tr><th scope="col" class="py-1 font-normal">Bucket start ({{ tz }})</th><th v-for="s in result.series" :key="s.name" scope="col" class="font-normal">{{ s.name }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.iso" class="border-t border-[var(--wolf-line)]">
              <th scope="row" class="wolf-data py-0.5 font-normal">{{ full(r.iso, tz, { zone: false }) }}</th>
              <td v-for="s in result.series" :key="s.name" class="wolf-data">{{ r[s.name] == null ? 'no data' : value(r[s.name]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
