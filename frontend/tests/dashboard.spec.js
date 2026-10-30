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
