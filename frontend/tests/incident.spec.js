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

describe('api call', () => {
  it('keeps GET arguments without URLSearchParams.size (Safari 16)', async () => {
    const { call } = await import('@/services/api')
    const fetchFn = vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: 1 })))
    const original = Object.getOwnPropertyDescriptor(URLSearchParams.prototype, 'size')
    Object.defineProperty(URLSearchParams.prototype, 'size', { get: () => undefined, configurable: true })
    try {
      await call('m', { incident: 'WINC-1', cursor: null }, { fetchFn })
    } finally {
      if (original) Object.defineProperty(URLSearchParams.prototype, 'size', original)
    }
    expect(fetchFn.mock.calls[0][0]).toBe('/api/method/m?incident=WINC-1')
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

describe('IncidentPage regulatory reporting', () => {
  it('lists the deadlines with their status and loads the report draft as text', async () => {
    const regulatory = {
      nis2: true, gdpr: false, reference: 'CERT-FR 2026-123',
      deadlines: [
        { id: 'nis2_early_warning', regime: 'NIS2', label: 'Early warning to the CSIRT / authority (24 h)',
          due_at: '2026-10-31T00:48:00Z', done_at: null, status: 'overdue', estimated: false },
        { id: 'nis2_final_report', regime: 'NIS2', label: 'Final report (1 month after the notification)',
          due_at: '2026-12-02T00:48:00Z', done_at: null, status: 'open', estimated: true },
      ],
    }  // prettier-ignore
    server({
      'frappe_wolf.api.incident_view.get_incident': () => ({ ...INCIDENT, regulatory }),
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
      'frappe_wolf.api.compliance.get_report': () => ({ markdown: '# Incident report: <b>x</b>' }),
    })
    const { page } = await open()
    const text = page.text()
    expect(text).toContain('Regulatory reporting: NIS2 significant incident')
    expect(text).toContain('reference CERT-FR 2026-123')
    expect(text).toContain('Overdue')
    expect(text).toContain('(estimated)')
    expect(text).toContain('sends nothing')
    await page.findAll('button').find((b) => b.text() === 'Report draft').trigger('click')
    await flushPromises()
    expect(page.get('pre').text()).toBe('# Incident report: <b>x</b>') // text, not HTML
    expect(page.find('pre b').exists()).toBe(false)
  })

  it('shows no regulatory section for an incident without a flag', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => ({ ...INCIDENT, regulatory: null }),
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
    })
    const { page } = await open()
    expect(page.text()).not.toContain('Regulatory reporting')
  })
})

describe('IncidentPage response actions', () => {
  it('asks for a second click, sends the approval and shows the recorded result', async () => {
    let actions = []
    const proposal = { action: 'block_ip', target: '45.33.32.156', agent: 'AG-1', host: 'bench-01', reason: 'ssh_failed_login: 25 failed',
      label: 'Block 45.33.32.156 for 24 h on bench-01' }  // prettier-ignore
    const fetchFn = server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
      'frappe_wolf.api.response.get_response': () => ({
        proposals: actions.length ? [] : [proposal], actions, can_approve: true, default_hours: 24, max_hours: 168,
      }),
      'frappe_wolf.api.response.approve': () => {
        actions = [{ name: 'WRA-00001', action: 'block_ip', target: '45.33.32.156', host: 'bench-01', status: 'Approved',
          approved_by: 'ops@example.com', approved_at: '2026-11-01T10:00:00Z', expires_at: '2026-11-02T10:00:00Z', result: null }]  // prettier-ignore
        return actions[0]
      },
    })
    const { page } = await open()
    expect(page.text()).toContain('Block 45.33.32.156 for 24 h on bench-01')
    const approvals = () => fetchFn.mock.calls.filter(([u]) => u.includes('response.approve'))
    await page.findAll('button').find((b) => b.text() === 'Approve…').trigger('click')
    expect(approvals()).toHaveLength(0) // the first click only asks
    await page.get('input[aria-label=Hours]').setValue(48)
    await page.findAll('button').find((b) => b.text() === 'Confirm').trigger('click')
    await flushPromises()
    const [, init] = approvals()[0]
    expect(JSON.parse(init.body)).toMatchObject({ action: 'block_ip', target: '45.33.32.156', agent: 'AG-1', duration_hours: 48 })
    expect(page.text()).toContain('Approved, waiting for the agent')
    expect(page.text()).not.toContain('Approve…')
  })

  it('tells a Wolf User that only a manager can approve', async () => {
    server({
      'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
      'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }),
      'frappe_wolf.api.response.get_response': () => ({
        proposals: [{ action: 'disable_user', target: 'x@example.com', reason: 'api_key_generated', label: 'Disable user x@example.com' }],
        actions: [], can_approve: false, default_hours: 24, max_hours: 168,
      }),
    })
    const { page } = await open()
    expect(page.text()).toContain('Only a Wolf Manager can approve these.')
    expect(page.findAll('button').some((b) => b.text() === 'Approve…')).toBe(false)
  })
})
