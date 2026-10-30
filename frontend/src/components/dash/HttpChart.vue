<script setup>
import { LineChart } from 'frappe-ui/charts'
import { computed } from 'vue'

import { axisLabel, clock } from '@/services/time'

// Requests, 5xx and 502 per bucket (interval counts summed). 502 is part of 5xx, so the lines
// are drawn side by side, never stacked. A bucket without samples stays a gap.
const props = defineProps({
  http: { type: Object, required: true },
  timeZone: { type: String, required: true },
})

const LABELS = { http_requests: 'Requests', http_5xx: '5xx', http_502: '502 (part of 5xx)' }
// windows over a day need the date on each label
const long = computed(() => new Date(props.http.window.until) - new Date(props.http.window.since) > 86400000)
const label = (iso) => axisLabel(iso, props.timeZone, long.value)

const rows = computed(() =>
  props.http.series.http_requests.map(([at], i) => ({
    at: label(at),
    [LABELS.http_requests]: props.http.series.http_requests[i][1],
    [LABELS.http_5xx]: props.http.series.http_5xx[i][1],
    [LABELS.http_502]: props.http.series.http_502[i][1],
  })),
)
// a deployment marks the bucket it fell in: "preceding change", not a cause. Past a few,
// labels pile up on each other: the lines stay, the list under the chart names them.
const LABELLED = 3
const markers = computed(() => {
  const starts = props.http.series.http_requests.map(([at]) => new Date(at).getTime())
  const labelled = props.http.deployments.length <= LABELLED
  const seen = new Set()
  return props.http.deployments.flatMap((d) => {
    const t = new Date(d.at).getTime()
    const i = starts.findLastIndex((s) => s <= t)
    if (i < 0 || seen.has(i)) return [] // one line per bucket
    seen.add(i)
    return [{ axis: 'x', value: rows.value[i].at, label: labelled ? `deploy ${clock(d.at, props.timeZone)}` : undefined, dashed: true }]
  })
})
const anyData = computed(() => Object.values(props.http.has_data).some(Boolean))
</script>

<template>
  <div class="wolf-card space-y-2">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-base font-semibold text-ink-gray-9">HTTP responses</h2>
      <span class="text-sm text-ink-gray-6">
        per {{ http.bucket_seconds / 60 }} min, read from {{ http.resolution }} data, times in {{ timeZone }}
      </span>
    </div>
    <div v-if="anyData" class="h-64">
      <LineChart :data="rows" x="at" :y="Object.values(LABELS)" :x-axis="{ type: 'category' }" :reference-lines="markers" />
    </div>
    <p v-else class="text-sm text-ink-gray-6">
      No HTTP counts in this window. The agent counts responses from nginx access logs; none arrived
      for this scope, which is not the same as no traffic.
    </p>
    <p class="text-sm text-ink-gray-6">
      502 responses are part of the 5xx count, not in addition to it. Breaks in a line are buckets without data.
      <template v-if="http.deployments.length"> Dashed lines are deployments<template v-if="http.deployments.length > LABELLED">, listed below</template>.</template>
    </p>
  </div>
</template>
