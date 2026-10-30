import { describe, expect, it } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import { makeRouter } from '@/router'

const router = makeRouter(createMemoryHistory('/wolf'))

describe('routes', () => {
  it.each([
    ['/', 'overview'],
    ['/incidents', 'incidents'],
    ['/incidents/WINC-00012', 'incident'],
    ['/events', 'events'],
    ['/errors/abc123', 'error-group'],
    ['/metrics', 'metrics'],
    ['/logs', 'logs'],
    ['/sites/a.example.com', 'site'],
    ['/hosts/bench-01', 'host'],
    ['/monitoring', 'monitoring'],
  ])('%s is %s', (path, name) => {
    expect(router.resolve(path).name).toBe(name)
  })

  it('addresses a bench through its host (bench names repeat across hosts)', () => {
    const route = router.resolve('/hosts/bench-01/benches/frappe-bench')
    expect(route.name).toBe('bench')
    expect(route.params).toEqual({ host: 'bench-01', bench: 'frappe-bench' })
    expect(router.resolve({ name: 'bench', params: { host: 'h2', bench: 'frappe-bench' } }).href).toBe(
      '/wolf/hosts/h2/benches/frappe-bench',
    )
  })

  it('answers unknown paths with the not-found page', () => {
    expect(router.resolve('/nope/at/all').name).toBe('not-found')
  })

  it('gives every view a title, and every pending view a plan', () => {
    for (const route of router.getRoutes()) {
      expect(route.meta.title, route.path).toBeTruthy()
      if (route.meta.fallback) expect(route.meta.planned, route.path).toBeTruthy()
    }
  })
})
