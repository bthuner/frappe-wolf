import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import { useTimeline } from '@/composables/useTimeline'
import IncidentPage from '@/pages/IncidentPage.vue'
import { makeRouter } from '@/router'

const BOOT = {
  time_zone: 'Europe/Paris',
  desk: { home: '/app', fallbacks: {} },
  capabilities: { incidents: { available: true }, logs: { available: false, reason: '' } },
}

const INCIDENT = {
  name: 'WINC-00001', title: '502 spike after deployment', status: 'Open', severity: 'critical',
  scope: { site: 'a.example.com', bench: 'frappe-bench', host: 'bench-01' },
  started_at: '2026-10-30T00:48:00Z', last_event_at: null,
  acknowledged: { at: null }, resolved: { at: null }, assigned_to: null, alert_rule: { name: '502 spike' },
  deployment: { at: '2026-10-30T00:47:00Z', event: 'E0', available: true }, silenced: false,
  merged_into: null, event_records: 2, event_count: 15, time_zone: 'Europe/Paris',
  can: { comment: true, acknowledge: true, resolve: true, assign_to_me: true, workflow: true },
}

const ROWS = [
  { id: 'event:E0', kind: 'deployment', family: 'context', relation: 'preceding_change', at: '2026-10-30T00:47:00Z',
    summary: 'bench update', scope: { site: 'a.example.com' }, count: 1, details: { task: 'update' } },
  { id: 'event:E1', kind: 'event', family: 'evidence', relation: 'linked', at: '2026-10-30T00:48:00Z', severity: 'critical',
    summary: '502 on GET /api', scope: { site: 'a.example.com' }, count: 14, last_seen: '2026-10-30T00:53:00Z', details: {} },
  { id: 'activity:comment:c1', kind: 'activity', family: 'activity', activity: 'comment', at: '2026-10-30T07:55:00Z',
    summary: 'Ops commented', user_name: 'Ops', text: '<img src=x onerror=alert(1)> looking' },
  { id: 'delivery:d1', kind: 'delivery', family: 'delivery', at: '2026-10-30T07:56:00Z', summary: 'slack opened accepted (200)',
    channel: 'slack', action: 'opened', result: 'accepted' },
]

function server(handlers) {
  const fetchFn = vi.fn(async (url, init) => {
    const method = url.split('/api/method/')[1].split('?')[0]
    const result = await handlers[method](url, init)
    if (result instanceof Response) return result
    return new Response(JSON.stringify({ message: result }), { status: 200 })
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

async function open(path = '/incidents/WINC-00001') {
  const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
  await router.push(path)
  await router.isReady()
  const page = mount(IncidentPage, { global: { plugins: [router], provide: { boot: BOOT } }, attachTo: document.body })
  await flushPromises()
  return { page, router }
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('IncidentPage', () => {
  it('shows the header, scope first, and every row family', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: ROWS, paging: { has_more: false }, totals: {}, truncated: {} }),
    })
    const { page } = await open()
    const text = page.text()
    expect(text).toContain('a.example.com · frappe-bench · bench-01')
    expect(text).toContain('Critical') // severity in words, not colour alone
    expect(text).toContain('Preceding change') // a deployment is never called the cause
    expect(text).toContain('14 reported') // a deduplicated record, not 14 rows
    expect(text).toContain('accepted by the endpoint') // not "read by someone"
    expect(text).toContain('7 h 07 min with no recorded activity') // silence, not "healthy"
    expect(text).toContain('2 records, 15 reported occurrences')
  })

  it('renders comments as text', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: ROWS, paging: { has_more: false }, totals: {}, truncated: {} }),
    })
    const { page } = await open()
    expect(page.find('img').exists()).toBe(false)
    expect(page.text()).toContain('<img src=x onerror=alert(1)> looking')
  })

  it('offers only the actions the server allows', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => ({ ...INCIDENT, can: { comment: true, acknowledge: false, resolve: false, assign_to_me: false, workflow: false } }),
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
    })
    const { page } = await open()
    const labels = page.findAll('button').map((b) => b.text())
    expect(labels).not.toContain('Acknowledge')
    expect(labels).not.toContain('Resolve')
    expect(page.text()).not.toContain('postmortem in Desk')
  })

  it('keeps the comment when posting fails, and shows why', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
      'frappe_wolf.api.incident.add_comment': () =>
        new Response(JSON.stringify({ _server_messages: JSON.stringify([JSON.stringify({ message: 'Not permitted' })]) }), { status: 403 }),
    })
    const { page } = await open()
    await page.get('textarea').setValue('restarting workers')
    await page.findAll('button').find((b) => b.text() === 'Comment').trigger('click')
    await flushPromises()
    expect(page.get('[role=alert]').text()).toBe('Not permitted')
    expect(page.get('textarea').element.value).toBe('restarting workers')
  })

  it('says a search only covers the loaded rows', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: ROWS, paging: { has_more: true, next_cursor: 'c' }, totals: {}, truncated: {} }),
    })
    const { page } = await open('/incidents/WINC-00001?q=502')
    expect(page.text()).toContain('1 of 4 loaded rows match; more rows are not loaded yet')
  })
})

describe('useTimeline', () => {
  it('drops an answer that arrives after a newer request', async () => {
    let release
    const slow = new Promise((r) => (release = r))
    const fetchFn = vi
      .fn()
      .mockImplementationOnce(async () => {
        await slow
        return new Response(JSON.stringify({ message: { rows: [ROWS[0]], paging: {}, totals: {}, truncated: {} } }))
      })
      .mockImplementationOnce(async () => new Response(JSON.stringify({ message: { rows: [ROWS[3]], paging: {}, totals: {}, truncated: {} } })))
    const t = useTimeline(() => 'WINC-00001', { fetchFn })
    const first = t.load(['evidence', 'context', 'activity', 'delivery'])
    await t.load(['delivery'])
    release()
    await first
    expect(t.rows.value.map((r) => r.id)).toEqual(['delivery:d1'])
  })

  it('never doubles a row when pages overlap', async () => {
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ message: { rows: ROWS.slice(0, 2), paging: { has_more: true, next_cursor: 'x' }, totals: {}, truncated: {} } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ message: { rows: ROWS.slice(1), paging: { has_more: false }, totals: {}, truncated: {} } })))
    const t = useTimeline(() => 'WINC-00001', { fetchFn })
    await t.load(['evidence'])
    await t.more(['evidence'])
    expect(t.rows.value.map((r) => r.id)).toEqual(ROWS.map((r) => r.id))
  })
})
