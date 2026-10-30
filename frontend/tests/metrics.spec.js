import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import MetricsPage from '@/pages/MetricsPage.vue'
import { makeRouter } from '@/router'

const BOOT = { time_zone: 'UTC', desk: { home: '/app', fallbacks: {} }, capabilities: { incidents: { available: true } } }
const CATALOG = [
  { metric: 'disk_used_percent', label: 'Disk used', unit: '%', kind: 'gauge', seen: true, label_key: 'mount',
    aggregations: ['avg', 'min', 'max', 'last'], default_aggregation: 'avg', group_bys: ['none', 'host', 'host_label'] },
  { metric: 'http_5xx', label: '5xx responses', unit: 'responses', kind: 'interval_count', seen: true, label_key: null,
    aggregations: ['sum', 'avg', 'max'], default_aggregation: 'sum', group_bys: ['none', 'host', 'bench', 'site'] },
]
const T = ['2026-10-30T10:00:00Z', '2026-10-30T10:01:00Z', '2026-10-30T10:02:00Z']
const RESULT = {
  metric: CATALOG[0], aggregation: 'max', resolution: 'raw', bucket_seconds: 60,
  window: { since: T[0], until: '2026-10-30T10:03:00Z' }, retention: { clipped: true, kept_from: '2026-10-28T10:00:00Z' },
  groups: { shown: 1, total: 4, ranked_by: 'max over the window, highest first' }, live: true,
  series: [{ name: 'h1 · mount=/data', value: 91, samples: 12, last_sample: T[2], stale: true, points: [[T[0], null], [T[1], 90], [T[2], 91]] }],
}

function server(overrides = {}) {
  const handlers = {
    'frappe_wolf.api.metrics.get_metric_catalog': () => ({ metrics: CATALOG }),
    'frappe_wolf.api.metrics.get_labels': () => ({ labels: ['mount=/', 'mount=/data'] }),
    'frappe_wolf.api.metrics.query_metric': () => RESULT,
    ...overrides,
  }
  const fetchFn = vi.fn(async (url) => {
    const method = url.split('/api/method/')[1].split('?')[0]
    return new Response(JSON.stringify({ message: await handlers[method](url) }))
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

let selectHandler
async function open(path) {
  const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
  await router.push(path)
  await router.isReady()
  const page = mount(MetricsPage, {
    global: {
      plugins: [router],
      provide: { boot: BOOT },
      stubs: { LineChart: { props: ['data'], emits: ['select'], setup(_p, { emit }) { selectHandler = (row) => emit('select', { row }); return () => null } } },
    },
  })
  await flushPromises()
  return { page, router }
}
const queries = (fetchFn) => fetchFn.mock.calls.map(([u]) => u).filter((u) => u.includes('query_metric'))

afterEach(() => vi.unstubAllGlobals())

describe('MetricsPage', () => {
  it('does not call a past window stale', async () => {
    server({ 'frappe_wolf.api.metrics.query_metric': () => ({ ...RESULT, live: false, series: [{ ...RESULT.series[0], stale: false }] }) })
    const { page } = await open(`/metrics?metric=disk_used_percent&since=${T[0]}&until=${T[2]}`)
    expect(page.text()).not.toContain('no new data')
    expect(page.text()).toContain('last sample in this window at')
  })

  it('asks only for what the catalog allows: a sum of disk levels falls back to the default', async () => {
    const fetchFn = server()
    await open('/metrics?metric=disk_used_percent&agg=sum&group=site')
    const url = queries(fetchFn).at(-1)
    expect(url).toContain('aggregation=avg')
    expect(url).toContain('group_by=none')
  })

  it('says what it read: bucket, resolution, clipping, groups left out, stale data, gaps', async () => {
    server()
    const { page } = await open('/metrics?metric=disk_used_percent&agg=max&group=host_label')
    const text = page.text()
    expect(text).toContain('max per 1 min')
    expect(text).toContain('read from raw data')
    expect(text).toContain('no longer kept at this resolution')
    expect(text).toContain('Showing 1 of 4 groups, max over the window, highest first')
    expect(text).toContain('no new data')
  })

  it('shows the data as a table, with gaps written as no data', async () => {
    server()
    const { page } = await open('/metrics?metric=disk_used_percent')
    await page.findAll('button').find((b) => b.text() === 'Show the data as a table').trigger('click')
    const cells = page.findAll('tbody td').map((c) => c.text())
    expect(cells).toEqual(['no data', '90 %', '91 %'])
  })

  it('turns two selected points into an explicit window in the URL', async () => {
    server()
    const { router } = await open('/metrics?metric=disk_used_percent&range=7d')
    selectHandler({ iso: T[2] })
    selectHandler({ iso: T[0] })
    await flushPromises()
    const q = router.currentRoute.value.query
    expect([q.since, q.until, q.range]).toEqual([T[0], '2026-10-30T10:03:00.000Z', undefined])
  })
})
