import { describe, expect, it, vi } from 'vitest'

import { deskFallback, loadBoot, logout } from '@/services/boot'

const BOOT = {
  user: { name: 'ops@example.com', full_name: 'Ops' },
  desk: { home: '/app', fallbacks: { incidents: '/app/wolf-incident', overview: '/app/wolf-overview' } },
}

function fakeWindow(extra = {}) {
  return { location: { pathname: '/wolf/incidents', search: '?status=Open', href: '' }, ...extra }
}

describe('loadBoot', () => {
  it('uses the boot data embedded in the page', async () => {
    const fetchFn = vi.fn()
    expect(await loadBoot({ win: fakeWindow({ wolf: BOOT }), fetchFn })).toBe(BOOT)
    expect(fetchFn).not.toHaveBeenCalled()
  })

  it('fetches it on the dev server', async () => {
    const win = fakeWindow()
    const fetchFn = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ message: BOOT }) })
    expect(await loadBoot({ win, fetchFn })).toEqual(BOOT)
    expect(fetchFn.mock.calls[0][0]).toBe('/api/method/frappe_wolf.api.spa.get_boot')
    expect(win.wolf).toEqual(BOOT)
  })

  it('sends a signed-out user to the login page and back', async () => {
    const win = fakeWindow()
    const fetchFn = vi.fn().mockResolvedValue({ ok: false, status: 403 })
    expect(await loadBoot({ win, fetchFn })).toBeNull()
    expect(win.location.href).toBe('/login?redirect-to=%2Fwolf%2Fincidents%3Fstatus%3DOpen')
  })

  it('fails loudly on a server error', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ ok: false, status: 500 })
    await expect(loadBoot({ win: fakeWindow(), fetchFn })).rejects.toThrow('HTTP 500')
  })
})

describe('deskFallback', () => {
  it('maps a view to its Desk page', () => {
    expect(deskFallback(BOOT, { meta: { fallback: 'overview' }, params: {} })).toBe('/app/wolf-overview')
  })

  it('links a record to its Desk form, escaped', () => {
    const route = {
      meta: { fallback: 'incidents', deskDoc: { doctype: 'wolf-incident', param: 'incident' } },
      params: { incident: 'WINC-00012/x' },
    }
    expect(deskFallback(BOOT, route)).toBe('/app/wolf-incident/WINC-00012%2Fx')
  })

  it('has nothing for a view without a Desk page', () => {
    expect(deskFallback(BOOT, { meta: {}, params: {} })).toBeNull()
  })
})

describe('signed in without a Wolf role', () => {
  it('shows an error instead of looping through /login', async () => {
    const win = fakeWindow({ document: { cookie: 'user_id=clerk%40example.com; sid=abc' } })
    const fetchFn = vi.fn().mockResolvedValue({ ok: false, status: 403 })
    await expect(loadBoot({ win, fetchFn })).rejects.toThrow('Wolf User or Wolf Manager role')
    expect(win.location.href).toBe('')
  })

  it('still sends a guest to the login page on 403', async () => {
    const win = fakeWindow({ document: { cookie: 'user_id=Guest' } })
    const fetchFn = vi.fn().mockResolvedValue({ ok: false, status: 403 })
    expect(await loadBoot({ win, fetchFn })).toBeNull()
    expect(win.location.href).toMatch(/^\/login\?redirect-to=/)
  })
})

describe('logout', () => {
  it('posts to the logout method with the CSRF token, then leaves', async () => {
    const win = fakeWindow({ csrf_token: 'tok' })
    const fetchFn = vi.fn().mockResolvedValue({ ok: true })
    await logout({ win, fetchFn })
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/method/logout')
    expect(init.method).toBe('POST')
    expect(init.headers['X-Frappe-CSRF-Token']).toBe('tok')
    expect(win.location.href).toBe('/login')
  })
})
