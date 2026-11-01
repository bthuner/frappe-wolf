import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import SecurityPage from '@/pages/SecurityPage.vue'
import { makeRouter } from '@/router'

const BOOT = { time_zone: 'Europe/Paris', desk: { home: '/app', fallbacks: {} }, capabilities: {} }

const POSTURE = {
  site: 'a.example.com', local: false, hosts: ['bench-01'], recent_hours: 3,
  score: 41, coverage: 59, passed: 2, failed: 2, unknown: 4,
  checks: [
    { id: 'tls', label: 'TLS certificates valid for 14 days or more', weight: 2, status: 'pass', evidence: '60 days left', fix: null },
    { id: 'two_factor', label: 'Two-factor authentication for System Managers', weight: 3, status: 'unknown',
      evidence: 'settings of this site are only readable on the site itself', fix: null },
    { id: 'exposure', label: 'Bench services not listening beyond loopback', weight: 3, status: 'fail',
      evidence: 'bench-01: 1 service(s)', fix: 'Bind MariaDB, Redis, socket.io and gunicorn to 127.0.0.1' },
  ],
}  // prettier-ignore

function server(handlers) {
  const fetchFn = vi.fn(async (url) => {
    const method = url.split('/api/method/')[1].split('?')[0]
    return new Response(JSON.stringify({ message: await handlers[method](url) }), { status: 200 })
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

async function open(path = '/security') {
  const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
  await router.push(path)
  await router.isReady()
  const page = mount(SecurityPage, { global: { plugins: [router], provide: { boot: BOOT } } })
  await flushPromises()
  return page
}

afterEach(() => vi.unstubAllGlobals())

describe('SecurityPage', () => {
  it('shows the score, failures first, evidence in words and the fix', async () => {
    const fetchFn = server({
      'frappe_wolf.api.security.list_sites': () => ['a.example.com'],
      'frappe_wolf.api.security.get_posture': () => POSTURE,
    })
    const page = await open('/security?site=a.example.com')
    expect(fetchFn.mock.calls.some(([u]) => u.includes('get_posture?site=a.example.com'))).toBe(true)
    const text = page.text()
    expect(text).toContain('41 / 100')
    expect(text).toContain('2 passed, 2 failed, 4 unknown')
    expect(text).toContain('Wolf could read 59% of the weight')
    expect(text).toContain('System Settings checks are only readable on the site itself')
    const rows = page.findAll('tbody tr').map((r) => r.text())
    expect(rows[0]).toContain('Failed') // failures first
    expect(rows[0]).toContain('Fix: Bind MariaDB')
    expect(rows[1]).toContain('Unknown') // never shown as passed
    expect(rows[2]).toContain('Passed')
  })

  it('says why a site is refused', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      if (url.includes('list_sites')) return new Response(JSON.stringify({ message: [] }))
      return new Response(JSON.stringify({}), { status: 403 })
    }))  // prettier-ignore
    const page = await open('/security?site=other.example.com')
    expect(page.get('[role=alert]').text()).toBe('You may not see this site.')
  })
})
