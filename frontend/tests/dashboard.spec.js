import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import DashboardPage from '@/pages/DashboardPage.vue'
import { makeRouter } from '@/router'

const BOOT = { time_zone: 'UTC', desk: { home: '/app', fallbacks: {} }, capabilities: { incidents: { available: true } } }
const NOW = new Date().toISOString()
const OLD = new Date(Date.now() - 40 * 60000).toISOString()

const COUNTS = {
  restricted: false,
  incidents: { unresolved: 60, critical_unresolved: 7, unacknowledged: 40, sites_with_unresolved: 3, unresolved_without_site: 5 },
  agents: { agent: { fresh: 3, stale: 1, never_seen: 0, disabled: 0 }, satellite: { fresh: 0, stale: 0, never_seen: 0, disabled: 0 } },
}
const HTTP = {
  window: { since: OLD, until: NOW }, bucket_seconds: 720, resolution: '1m', deployments: [],
  series: { http_requests: [[OLD, 10], [NOW, null]], http_5xx: [[OLD, 2], [NOW, null]], http_502: [[OLD, 1], [NOW, null]] },
  has_data: { http_requests: true, http_5xx: true, http_502: true },
}
const SIGNALS = {
  series: [
    { metric_name: 'disk_used_percent', group: 'host', label: 'Disk used', unit: '%', host: 'h1', label_key: 'mount=/', value: 93, last_at: NOW, state: 'firing',
      rules: [{ name: 'R1', rule_name: 'Disk almost full', aggregation: 'max', operator: '>', threshold: 90, window_minutes: 5 }], incidents: ['WINC-00009'] },
    { metric_name: 'cpu_usage', group: 'host', label: 'CPU', unit: '%', host: 'h1', value: 12, last_at: OLD, state: 'stale', rules: [], incidents: [] },
  ],
  counts: { host: { firing: 1, stale: 1 } },
  missing: [{ metric: 'backup_age_hours', label: 'Newest backup age' }],
  caveats: {}, email: {}, lookback_days: 7, truncated: false, total_series: 2,
}

function server(overrides = {}) {
  const handlers = {
    'frappe_wolf.api.scope.get_counts': () => COUNTS,
    'frappe_wolf.api.dashboard.get_http': () => HTTP,
    'frappe_wolf.api.dashboard.get_signals': () => SIGNALS,
    'frappe_wolf.api.incident_view.list_incidents': () => ({ incidents: [], paging: { has_more: false } }),
    'frappe_wolf.api.scope.get_inventory': () => ({ hosts: [], note: '' }),
    'frappe_wolf.api.dashboard.get_monitoring': () => ({ health: null, rollups: {}, agents: [], silence_minutes: 5, mode: { mode: 'central' }, note: '' }),
    ...overrides,
  }
  const fetchFn = vi.fn(async (url) => {
    const method = url.split('/api/method/')[1].split('?')[0]
    try {
      return new Response(JSON.stringify({ message: await handlers[method](url) }))
    } catch (e) {
      return new Response(JSON.stringify({ exception: `frappe.Error: ${e.message}` }), { status: 500 })
    }
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

async function open(path = '/') {
  const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
  await router.push(path)
  await router.isReady()
  const page = mount(DashboardPage, {
    global: { plugins: [router], provide: { boot: BOOT }, stubs: { LineChart: { template: '<div class="chart-stub" />' } } },
  })
  await flushPromises()
  return { page, router }
}

afterEach(() => vi.unstubAllGlobals())

describe('HttpChart labels', () => {
  it('stay unique over a whole day, so a deployment lands in its own bucket', async () => {
    const { default: HttpChart } = await import('@/components/dash/HttpChart.vue')
    const start = Date.parse('2026-10-29T10:36:00Z')
    const points = Array.from({ length: 121 }, (_, i) => [new Date(start + i * 720000).toISOString(), i])
    const http = { ...HTTP, bucket_seconds: 720, series: { http_requests: points, http_5xx: points, http_502: points },
      window: { since: points[0][0], until: points[120][0] },
      deployments: [{ name: 'D1', at: new Date(start + 120 * 720000 + 60000).toISOString() }] }
    let captured
    const page = mount(HttpChart, {
      props: { http, timeZone: 'UTC' },
      global: { stubs: { LineChart: { props: ['data', 'referenceLines'], setup(p) { captured = p; return () => null } } } },
    })
    await flushPromises()
    const labels = captured.data.map((r) => r.at)
    expect(new Set(labels).size).toBe(labels.length)
    expect(captured.referenceLines.map((l) => l.value)).toEqual([labels[120]])
    page.unmount()
  })
})

describe('DashboardPage', () => {
  it('shows exact counts, not the length of a list', async () => {
    server()
    const { page } = await open()
    expect(page.text()).toContain('60')
    expect(page.text()).toContain('7 critical')
    expect(page.text()).toContain('+ 5 host or bench-wide')
  })

  it('keeps signal states apart and never shows missing signals as zero', async () => {
    server()
    const { page } = await open()
    const text = page.text()
    expect(text).toContain('Rule firing')
    expect(text).toContain('Disk almost full: max > 90 over 5 min') // the rule's threshold, not the UI's
    expect(text).toContain('No new data')
    expect(text).toContain('last sample 40 min ago')
    expect(text).not.toMatch(/CPU\s+12 %/) // a stale value is not shown as current
    expect(text).toContain('1 signals without data in the last 7 days')
    expect(text).toContain('They are not zero')
  })

  it('says 502 is part of 5xx, and that no HTTP data is not no traffic', async () => {
    server({ 'frappe_wolf.api.dashboard.get_http': () => ({ ...HTTP, has_data: { http_requests: false, http_5xx: false, http_502: false } }) })
    const { page } = await open()
    expect(page.text()).toContain('502 responses are part of the 5xx count')
    expect(page.text()).toContain('which is not the same as no traffic')
    expect(page.find('.chart-stub').exists()).toBe(false)
  })

  it('shows the other panels when one fails', async () => {
    server({ 'frappe_wolf.api.dashboard.get_signals': () => { throw new Error('boom') } })
    const { page } = await open()
    expect(page.text()).toContain('Signals: boom')
    expect(page.text()).toContain('7 critical')
  })

  it('draws the first 10 rows of a group and the rest on demand, problems first', async () => {
    const row = (i, state) => ({ metric_name: 'cpu_usage', group: 'host', label: 'CPU', unit: '%', host: `h${String(i).padStart(2, '0')}`,
      value: i, last_at: state === 'stale' ? OLD : NOW, state, rules: [], incidents: [] })
    // the server's order: firing, then stale, then the others
    const series = [...Array.from({ length: 9 }, (_, i) => row(i, 'firing')), ...Array.from({ length: 3 }, (_, i) => row(9 + i, 'stale')),
      ...Array.from({ length: 13 }, (_, i) => row(12 + i, 'unwatched'))]
    server({ 'frappe_wolf.api.dashboard.get_signals': () => ({ ...SIGNALS, series, counts: { host: { firing: 9, stale: 3, unwatched: 13 } }, total_series: 25 }) })
    const { page } = await open()
    const rows = () => page.findAll('[aria-label="Signals"] li')
    expect(rows()).toHaveLength(10)
    expect(page.text()).toContain('· 9 rule firing · 3 no new data') // the group's counts stay exact
    const more = page.findAll('button').find((b) => b.text().startsWith('Show the other'))
    expect(more.text()).toBe('Show the other 15 of 25, 2 with a problem')
    await more.trigger('click')
    expect(rows()).toHaveLength(25)
    const fold = page.findAll('button').find((b) => b.text() === 'Show the first 10 only')
    await fold.trigger('click')
    expect(rows()).toHaveLength(10)
  })

  it('draws the first 20 hosts and the rest on demand', async () => {
    const hosts = Array.from({ length: 23 }, (_, i) => ({ host: `host-${i}`, sources: ['agent'], benches: [] }))
    server({ 'frappe_wolf.api.scope.get_inventory': () => ({ hosts, note: '' }) })
    const { page } = await open()
    const drawn = () => page.findAll('[aria-label="Hosts, benches and sites"] > ul > li')
    expect(drawn()).toHaveLength(20)
    const more = page.findAll('button').find((b) => b.text() === 'Show the other 3 of 23 hosts')
    await more.trigger('click')
    expect(drawn()).toHaveLength(23)
  })

  it('scopes a bench by its host and keeps the range in the URL', async () => {
    const fetchFn = server()
    const { router } = await open('/hosts/h2/benches/frappe-bench?range=7d')
    const counts = fetchFn.mock.calls.map(([url]) => url).find((u) => u.includes('get_counts'))
    expect(counts).toContain('host=h2')
    expect(counts).toContain('bench=frappe-bench')
    expect(counts).toContain('range=7d')
    expect(router.currentRoute.value.query.range).toBe('7d')
  })
})

describe('MonitoringPanel', () => {
  const MONITORING = (tick) => ({
    health: {
      rules: { last_evaluated: NOW, age_seconds: 30 }, problems: [],
      scheduler: { enabled: true, reason: null, last_job_run: NOW, age_seconds: 30, tick_seconds: tick },
      queues: { ingest_backlog_seconds: null, workers: 2 },
    },
    rollups: {}, agents: [], silence_minutes: 5, mode: { mode: 'central' },
  })

  it("says how often jobs run, and what Frappe's 4-minute default means for alert rules", async () => {
    const { default: MonitoringPanel } = await import('@/components/dash/MonitoringPanel.vue')
    const slow = mount(MonitoringPanel, { props: { monitoring: MONITORING(240), timeZone: 'UTC' } })
    expect(slow.text()).toContain('enabled, jobs every 4 min')
    expect(slow.text()).toContain('Alert rules are evaluated at that pace, not every minute')
    expect(slow.text()).toContain('scheduler_tick_interval')
    const fast = mount(MonitoringPanel, { props: { monitoring: MONITORING(60), timeZone: 'UTC' } })
    expect(fast.text()).toContain('enabled, jobs every 1 min')
    expect(fast.text()).not.toContain('scheduler_tick_interval')
    // an older Frappe without the setting: nothing claimed
    const unknown = mount(MonitoringPanel, { props: { monitoring: MONITORING(null), timeZone: 'UTC' } })
    expect(unknown.text()).not.toContain('jobs every')
  })
})
