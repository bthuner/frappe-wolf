import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import ErrorGroupPage from '@/pages/ErrorGroupPage.vue'
import ErrorsPage from '@/pages/ErrorsPage.vue'
import EventsPage from '@/pages/EventsPage.vue'
import { makeRouter } from '@/router'

const BOOT = { time_zone: 'UTC', desk: { home: '/app', fallbacks: {} }, capabilities: { incidents: { available: true } } }
const AT = '2026-10-30T10:00:00Z'
const EVENT = { name: 'E1', title: 'KeyError: customer', event_type: 'error', severity: 'critical', count: 14, first_seen: AT,
  last_seen: '2026-10-30T10:30:00Z', site: 'a.example.com', bench: 'b1', host: 'h1', incident: 'WINC-00003', fingerprint: 'fp1' }

function server(handlers) {
  const fetchFn = vi.fn(async (url) => {
    const method = url.split('/api/method/')[1].split('?')[0]
    const result = await handlers[method](url)
    return result instanceof Response ? result : new Response(JSON.stringify({ message: result }))
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

async function open(component, path) {
  const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
  await router.push(path)
  await router.isReady()
  const page = mount(component, { global: { plugins: [router], provide: { boot: BOOT } } })
  await flushPromises()
  return { page, router }
}

afterEach(() => vi.unstubAllGlobals())

describe('EventsPage', () => {
  it('passes the URL filters to the server and shows a record as one row with its count', async () => {
    const fetchFn = server({
      'frappe_wolf.api.explore.list_events': () => ({ events: [EVENT], paging: { has_more: false } }),
    })
    const { page } = await open(EventsPage, '/events?type=error&severity=critical&host=h1&bench=b1&q=Key&range=7d')
    const url = fetchFn.mock.calls[0][0]
    for (const part of ['event_type=error', 'severity=critical', 'host=h1', 'bench=b1', 'q=Key', 'range=7d']) expect(url).toContain(part)
    expect(page.text()).toContain('14 reported, last 10:30')
    expect(page.text()).toContain('inside the window or not')
  })

  it('drops a bench without its host', async () => {
    const fetchFn = server({ 'frappe_wolf.api.explore.list_events': () => ({ events: [], paging: { has_more: false } }) })
    await open(EventsPage, '/events?bench=b1')
    expect(fetchFn.mock.calls[0][0]).not.toContain('bench=')
  })

  it('shows a payload as text when a row opens', async () => {
    server({
      'frappe_wolf.api.explore.list_events': () => ({ events: [EVENT], paging: { has_more: false } }),
      'frappe_wolf.api.explore.get_event': () => ({ ...EVENT, payload: { traceback: '<script>alert(1)</script>' } }),
    })
    const { page } = await open(EventsPage, '/events')
    await page.findAll('button').find((b) => b.text() === 'Details').trigger('click')
    await flushPromises()
    expect(page.find('script').exists()).toBe(false)
    expect(page.get('pre').text()).toContain('<script>alert(1)</script>')
  })
})

describe('ErrorsPage', () => {
  it('keeps records and reported occurrences apart, and says occurrences are not a window count', async () => {
    server({
      'frappe_wolf.api.explore.list_error_groups': () => ({
        groups: [{ fingerprint: 'fp1', title: 'KeyError', severity: 'critical', records: 8, occurrences: 47, sites: 2, host_wide: false,
          incidents: 1, first_seen: AT, last_seen: AT, latest: { site: 'a.example.com' } }],
        paging: { has_more: false },
      }),
    })
    const { page } = await open(ErrorsPage, '/errors')
    const cells = page.findAll('tbody td').map((td) => td.text())
    expect(cells).toContain('8')
    expect(cells).toContain('47')
    expect(page.text()).toContain('they are not a count for the window')
  })
})

describe('ErrorGroupPage', () => {
  it('shows the newest traceback as text and the scopes it hit', async () => {
    server({
      'frappe_wolf.api.explore.get_error_group': () => ({
        fingerprint: 'fp1', title: 'KeyError', severity: 'critical', records: 8, occurrences: 47, first_seen: AT, last_seen: AT,
        scopes: [{ site: 'a.example.com', bench: 'b1', host: 'h1', records: 2, occurrences: 41, first_seen: AT, last_seen: AT }],
        incidents: [], incidents_not_visible: 1,
        representative: { ...EVENT, payload: { traceback: 'Traceback\n  KeyError: <b>customer</b>' } },
        recent_records: [EVENT], more_records: true,
      }),
    })
    const { page } = await open(ErrorGroupPage, '/errors/fp1')
    expect(page.find('b').exists()).toBe(false)
    expect(page.text()).toContain('KeyError: <b>customer</b>')
    expect(page.text()).toContain('and 1 you may not see')
    expect(page.text()).toContain('Older records are not listed; the counts above include them.')
  })

  it('says when the group is not visible', async () => {
    server({ 'frappe_wolf.api.explore.get_error_group': () => new Response('{}', { status: 404 }) })
    const { page } = await open(ErrorGroupPage, '/errors/nope')
    expect(page.text()).toContain('No error group with this fingerprint that you can see.')
  })
})
