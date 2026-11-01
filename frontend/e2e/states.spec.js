import { expect, test } from '@playwright/test'

import { BOOT, fail, fixtures, mockApi } from './api.js'

// Every view in every state it can be in, in Chromium on the production build: loading, empty,
// filtered-empty, error, forbidden, partial, stale and disconnected. A view never claims
// "nothing" before it has read, nor after a read failed.
const F = fixtures()
const slow = (method) => async (args) => {
  await new Promise((r) => setTimeout(r, 2500))
  return F[method](args)
}

// view, path, the read that fills it, its empty answer and what it then says
const VIEWS = [
  { name: 'overview', path: '/wolf/', read: 'frappe_wolf.api.scope.get_counts', denied: 'You may not see these counts.' },
  { name: 'incidents', path: '/wolf/incidents', read: 'frappe_wolf.api.incident_view.list_incidents',
    empty: () => ({ incidents: [], paging: { has_more: false } }), says: 'No unresolved incidents you can see.',
    denied: 'You may not see these incidents.' },
  { name: 'incident', path: '/wolf/incidents/WINC-00001', read: 'frappe_wolf.api.incident_view.get_incident',
    denied: 'You may not see this incident.' },
  { name: 'timeline', path: '/wolf/incidents/WINC-00001', read: 'frappe_wolf.api.incident_view.get_timeline',
    empty: () => ({ rows: [], paging: { has_more: false }, totals: {}, truncated: {} }), says: 'Nothing recorded in this window.',
    denied: "You may not see this incident's timeline." },
  { name: 'events', path: '/wolf/events', read: 'frappe_wolf.api.explore.list_events',
    empty: () => ({ events: [], paging: { has_more: false } }), says: 'No event records match.', denied: 'You may not see these records.' },
  { name: 'errors', path: '/wolf/errors', read: 'frappe_wolf.api.explore.list_error_groups',
    empty: () => ({ groups: [], paging: { has_more: false } }), says: 'No error groups match.', denied: 'You may not see these records.' },
  { name: 'metrics', path: '/wolf/metrics?metric=disk_used_percent', read: 'frappe_wolf.api.metrics.query_metric',
    empty: (a) => ({ ...F['frappe_wolf.api.metrics.query_metric'](a), series: [] }),
    says: 'No samples of this metric for this scope and window. That is no data, not zero.', denied: 'You may not see this metric.' },
  { name: 'monitoring', path: '/wolf/monitoring', read: 'frappe_wolf.api.dashboard.get_monitoring',
    denied: "You may not see Wolf's own monitoring." },
  { name: 'security', path: '/wolf/security', read: 'frappe_wolf.api.security.get_posture', denied: 'You may not see this site.' },
] // prettier-ignore

const NOTHING = /No unresolved incidents|Nothing recorded|No event records|No error groups|No samples of this metric/
const CLAIMS = ['No unresolved incidents', 'Nothing recorded', 'No event records', 'No error groups', 'No samples of this metric']

// Records every "nothing" claim the page ever shows, even for a single frame.
async function watchClaims(page) {
  await page.addInitScript((claims) => {
    window.__claims = []
    new MutationObserver(() => {
      const text = document.body?.innerText || ''
      for (const c of claims) if (text.includes(c) && !window.__claims.includes(c)) window.__claims.push(c)
    }).observe(document, { subtree: true, childList: true, characterData: true })
  }, CLAIMS)
}
const claims = (page) => page.evaluate(() => window.__claims)

for (const view of VIEWS) {
  test.describe(view.name, () => {
    test('loading says so, and claims nothing yet', async ({ page }) => {
      await watchClaims(page)
      await mockApi(page, { [view.read]: slow(view.read) })
      await page.goto(view.path)
      await expect(page.getByRole('status').filter({ hasText: 'Loading' }).first()).toBeVisible()
      await page.waitForTimeout(1500) // still reading
      expect(await claims(page), 'a "nothing" claim while reading').toEqual([])
    })

    if (view.empty) {
      test('empty says what is empty', async ({ page }) => {
        await mockApi(page, { [view.read]: view.empty })
        await page.goto(view.path)
        await expect(page.getByText(view.says)).toBeVisible()
        await expect(page.getByRole('status').filter({ hasText: 'Loading' })).toHaveCount(0)
      })
    }

    test('an error says why, and claims nothing', async ({ page }) => {
      await watchClaims(page)
      await mockApi(page, { [view.read]: () => fail(500, 'database is down') })
      await page.goto(view.path)
      await expect(page.getByRole('alert').filter({ hasText: 'database is down' }).first()).toBeVisible()
      expect(await claims(page), 'a "nothing" claim after a failed read').toEqual([])
    })

    test('a refusal says what may not be seen', async ({ page }) => {
      await mockApi(page, { [view.read]: () => fail(403, 'Not permitted') })
      await page.goto(view.path)
      await expect(page.getByText(view.denied).first()).toBeVisible()
      await expect(page.getByText('Not permitted')).toHaveCount(0) // Frappe's text, not the reader's
      await expect(page.getByText(NOTHING)).toHaveCount(0)
    })
  })
}

test('an ended session asks to log in again, not "you may not see"', async ({ page }) => {
  await mockApi(page, { 'frappe_wolf.api.incident_view.list_incidents': () => fail(403, 'Not permitted', { expired: true }) })
  await page.goto('/wolf/incidents')
  await expect(page.getByRole('alert')).toHaveText('Your session has ended: reload the page to log in again.')
})

test('the overview says when its monitoring panel fails, and the metric catalog while it loads', async ({ page }) => {
  await mockApi(page, {
    'frappe_wolf.api.dashboard.get_monitoring': () => fail(500, 'redis unreachable'),
    'frappe_wolf.api.metrics.get_metric_catalog': slow('frappe_wolf.api.metrics.get_metric_catalog'),
  })
  await page.goto('/wolf/')
  await expect(page.getByRole('alert').filter({ hasText: 'Monitoring: redis unreachable' })).toBeVisible()
  await page.goto('/wolf/metrics')
  await expect(page.getByRole('status').filter({ hasText: 'Loading the metric catalog' })).toBeVisible()
})

test('a slow timeline never says "Nothing recorded" before it has read', async ({ page }) => {
  await watchClaims(page)
  await mockApi(page, { 'frappe_wolf.api.incident_view.get_timeline': slow('frappe_wolf.api.incident_view.get_timeline') })
  await page.goto('/wolf/incidents/WINC-00001')
  await expect(page.getByText('restarting workers')).toBeVisible({ timeout: 10000 })
  expect(await claims(page)).toEqual([])
})

test('filtered-empty is not the same as empty', async ({ page }) => {
  await mockApi(page, { 'frappe_wolf.api.incident_view.list_incidents': () => ({ incidents: [], paging: { has_more: false } }) })
  await page.goto('/wolf/incidents?status=resolved')
  await expect(page.getByText('No resolved incidents you can see.')).toBeVisible()
  await mockApi(page)
  await page.goto('/wolf/incidents/WINC-00001?q=no-such-row')
  await expect(page.getByText('No rows match this filter.')).toBeVisible()
  await expect(page.getByText('Nothing recorded in this window.')).toHaveCount(0)
})

test('partial results say they are partial', async ({ page }) => {
  const list = F['frappe_wolf.api.incident_view.list_incidents']()
  await mockApi(page, {
    'frappe_wolf.api.incident_view.list_incidents': () => ({ ...list, paging: { has_more: true, next_cursor: 'c1' } }),
    'frappe_wolf.api.metrics.query_metric': (a) => ({
      ...F['frappe_wolf.api.metrics.query_metric'](a), groups: { shown: 1, total: 3, ranked_by: 'max over the window, highest first' },
    }),
    'frappe_wolf.api.incident_view.get_timeline': (a) => ({
      ...F['frappe_wolf.api.incident_view.get_timeline'](a), truncated: { activity_comment: { shown: 100, total: 140 } },
    }),
  })
  await page.goto('/wolf/incidents')
  await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible()
  await page.goto('/wolf/metrics?metric=disk_used_percent')
  await expect(page.getByText('Showing 1 of 3 groups')).toBeVisible()
  await page.goto('/wolf/incidents/WINC-00001')
  await expect(page.getByText('Activity shows the newest 100 of 140 comments')).toBeVisible()
})

test('stale data says no new data, never a value as if fresh', async ({ page }) => {
  const q = F['frappe_wolf.api.metrics.query_metric']
  await mockApi(page, {
    'frappe_wolf.api.metrics.query_metric': (a) => ({ ...q(a), series: q(a).series.map((s) => ({ ...s, stale: true })) }),
  })
  await page.goto('/wolf/metrics?metric=disk_used_percent')
  await expect(page.getByText(/no new data, last sample/)).toBeVisible()
})

test('disconnected: a refused socket falls back to polling, and says so', async ({ page }) => {
  const live = { available: true, namespace: '/wolf.localhost', port: 9000, dev_server: false, poll_seconds: 60 }
  await mockApi(page, { 'frappe_wolf.api.spa.get_boot': () => ({ ...BOOT, capabilities: { ...BOOT.capabilities, realtime: live } }) })
  await page.goto('/wolf/incidents') // mockApi aborts every socket.io request
  await expect(page.locator('.wolf-live')).toHaveText('Checks every 60 s', { timeout: 15000 })
})
