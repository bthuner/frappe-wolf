import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { fixtures, INCIDENT, mockApi } from './api.js'

// Every view of the Wolf UI, in a real browser on the production build: it renders, no console
// error, no API call the page was not given, and no serious or critical WCAG 2 A/AA finding in
// the light or the dark theme.
// the text each view must show from its own data: never a label the sidebar also has
const PAGES = [
  ['/wolf/', '502 spike after deployment: a.example.com'],
  ['/wolf/incidents', '502 spike after deployment: a.example.com'],
  ['/wolf/incidents/WINC-00001', 'restarting workers'],
  ['/wolf/events', 'KeyError: customer'],
  ['/wolf/errors', 'KeyError: customer'],
  ['/wolf/metrics?metric=disk_used_percent', 'bench-01 · mount=/'],
  ['/wolf/logs', 'Log search is not available'],
  ['/wolf/monitoring', 'Unknown: the site could not compute its health.'],
  ['/wolf/security', '71 / 100'],
]

function watchConsole(page) {
  const errors = []
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
  page.on('pageerror', (e) => errors.push(String(e)))
  return errors
}

async function axe(page) {
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  return result.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`)
}

for (const scheme of ['light', 'dark']) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme })

    for (const [path, text] of PAGES) {
      test(`${path} renders and passes axe`, async ({ page }) => {
        const errors = watchConsole(page)
        const calls = await mockApi(page)
        await page.goto(path)
        await expect(page.getByText(text, { exact: false }).first()).toBeVisible()
        await page.waitForLoadState('networkidle')
        expect(await page.locator('html').getAttribute('data-theme')).toBe(scheme)
        expect(errors, 'console errors').toEqual([])
        expect(calls.filter((c) => !c.mocked).map((c) => c.method), 'calls the test did not answer').toEqual([])
        expect(await axe(page), 'serious or critical accessibility findings').toEqual([])
      })
    }
  })
}

test('keyboard: the sidebar is reachable with Tab and a link opens with Enter', async ({ page }) => {
  await mockApi(page)
  await page.goto('/wolf/')
  await expect(page.getByText('Overview').first()).toBeVisible()
  let reached = false
  for (let i = 0; i < 40 && !reached; i++) {
    await page.keyboard.press('Tab')
    reached = await page.evaluate(() => document.activeElement?.textContent?.trim() === 'Incidents')
  }
  expect(reached, 'Tab reaches the Incidents link').toBe(true)
  // a ring that shows: an outline with a width, or a box-shadow with a visible colour and spread
  const outline = await page.evaluate(() => {
    const s = getComputedStyle(document.activeElement)
    const outlined = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0
    const shadows = s.boxShadow === 'none' ? [] : s.boxShadow.split(/,(?![^(]*\))/)
    const ring = shadows.some((sh) => !/rgba\([^)]*,\s*0\)/.test(sh) && /\s[1-9]\d*(\.\d+)?px/.test(sh.replace(/rgba?\([^)]*\)/, '')))
    return outlined || ring
  })
  expect(outline, 'the focused link shows a focus ring').toBe(true)
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/\/wolf\/incidents$/)
})

test('incident: acknowledge posts once and the header follows', async ({ page }) => {
  const errors = watchConsole(page)
  const calls = await mockApi(page)
  await page.goto('/wolf/incidents/WINC-00001')
  await page.getByRole('button', { name: 'Acknowledge' }).click()
  await expect(page.getByText('Acknowledged').first()).toBeVisible()
  const acts = calls.filter((c) => c.method === 'frappe_wolf.api.incident_view.act')
  expect(acts.map((c) => c.args)).toEqual([{ incident: 'WINC-00001', action: 'acknowledge' }])
  await expect(page.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0)
  await page.waitForLoadState('networkidle') // the timeline reload after the action
  expect(errors, 'console errors').toEqual([])
  expect(calls.filter((c) => !c.mocked).map((c) => c.method), 'calls the test did not answer').toEqual([])
  await expect(page.locator('[role=alert]')).toHaveCount(0)
})

test('reduced motion: no animation runs longer than a frame', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockApi(page)
  await page.goto('/wolf/incidents/WINC-00001')
  await expect(page.getByText('restarting workers')).toBeVisible()
  const slow = await page.evaluate(() =>
    [...document.querySelectorAll('*')].filter((el) => {
      const s = getComputedStyle(el)
      const secs = (v) => Math.max(...v.split(',').map((x) => parseFloat(x) * (x.trim().endsWith('ms') ? 0.001 : 1)))
      return secs(s.animationDuration) > 0.02 || secs(s.transitionDuration) > 0.02
    }).length,
  )
  expect(slow).toBe(0)
})

// Focus restoration: where keyboard focus goes after an action, as Chromium really moves it.
const focused = (page) =>
  page.evaluate(() => {
    const a = document.activeElement
    return a === document.body ? 'body' : `${a.tagName.toLowerCase()}:${(a.getAttribute('aria-label') || a.textContent).trim()}`
  })

test('focus: a button that works for a moment keeps the focus', async ({ page }) => {
  const slow = fixtures()['frappe_wolf.api.incident_view.get_timeline']
  await mockApi(page, {
    'frappe_wolf.api.incident_view.get_timeline': async (args) => {
      await new Promise((r) => setTimeout(r, 300)) // long enough for Chromium to drop the focus
      return slow(args)
    },
  })
  await page.goto('/wolf/incidents/WINC-00001')
  await expect(page.getByText('restarting workers')).toBeVisible()
  const refresh = page.getByRole('button', { name: 'Refresh' })
  await refresh.focus()
  await page.keyboard.press('Enter')
  await expect(refresh).toBeDisabled() // frappe-ui disables a loading button
  await expect(refresh).toBeEnabled()
  await expect.poll(() => focused(page)).toBe('button:Refresh')
})

test('focus: an action that goes away hands the focus to the next one', async ({ page }) => {
  await mockApi(page)
  await page.goto('/wolf/incidents/WINC-00001')
  await page.getByRole('button', { name: 'Acknowledge' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0)
  await expect.poll(() => focused(page)).toBe('button:Assign to me')
})

test('focus: after a comment the box keeps the focus', async ({ page }) => {
  const calls = await mockApi(page, { 'frappe_wolf.api.incident.add_comment': () => ({}) })
  await page.goto('/wolf/incidents/WINC-00001')
  const box = page.getByRole('textbox', { name: 'Comment' })
  await box.fill('Restarted the workers')
  await page.getByRole('button', { name: 'Comment', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(box).toHaveValue('')
  await expect(box).toBeFocused()
  expect(calls.filter((c) => c.method === 'frappe_wolf.api.incident.add_comment')).toHaveLength(1)
})

test('focus: a new page focuses its title; a filter or the first load does not', async ({ page }) => {
  await mockApi(page)
  await page.goto('/wolf/')
  await expect(page.getByText('502 spike after deployment: a.example.com').first()).toBeVisible()
  expect(await focused(page)).toBe('body') // the first load leaves focus where the browser puts it
  await page.getByRole('link', { name: 'Incidents' }).first().focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/\/wolf\/incidents$/)
  await expect.poll(() => focused(page)).toBe('h1:Incidents')
  expect(await page.locator('h1').count()).toBe(1)
  await expect(page.getByText('502 spike after deployment: a.example.com').first()).toBeVisible()
  // a status filter only changes the query: focus stays on it (Space checks a radio)
  await page.getByRole('radio', { name: 'Resolved', exact: true }).focus()
  await page.keyboard.press('Space')
  await expect(page).toHaveURL(/status=resolved/)
  expect(await focused(page)).toContain('Resolved')
  await page.goBack() // the filter replaced the URL: one step back is the overview
  await expect(page).toHaveURL(/\/wolf\/$/)
  await expect.poll(() => focused(page)).toMatch(/^h1:/)
})

test('merge: the dialog says what moves, passes axe, gives focus back, and merges on confirmation', async ({ page }) => {
  const card = (name) => ({ name, title: `incident ${name}`, status: 'Open', severity: 'warning',
    scope: { site: 'a.example.com', bench: 'frappe-bench', host: 'bench-01' }, started_at: INCIDENT.started_at }) // prettier-ignore
  const calls = await mockApi(page, {
    'frappe_wolf.api.incident_view.get_incident': () => ({ ...INCIDENT, can: { ...INCIDENT.can, merge: true, split: true } }),
    'frappe_wolf.api.incident_view.merge_preview': (args) => ({
      source: card('WINC-00001'), candidates: [card('WINC-00002')], more_candidates: false,
      preview: args.target ? { error: null, target: card('WINC-00002'), events: 2, comments: 1,
        target_after: { severity: 'critical', started_at: INCIDENT.started_at, status: 'Open' },
        source_after: { status: 'Resolved', merged_into: 'WINC-00002' } } : null,
    }), // prettier-ignore
    'frappe_wolf.api.incident.merge': () => ({ target: 'WINC-00002', events: 2, comments: 1 }),
  })
  await page.goto('/wolf/incidents/WINC-00001')
  const trigger = page.getByRole('button', { name: 'Merge…' })
  await trigger.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('combobox').selectOption('WINC-00002')
  await expect(dialog.getByText('Moves 2 events and 1 comment from WINC-00001 to WINC-00002')).toBeVisible()
  expect(await axe(page), 'serious or critical accessibility findings with the dialog open').toEqual([])
  // cancelling changes nothing and hands the focus back to the button that opened it
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expect(calls.filter((c) => c.method === 'frappe_wolf.api.incident.merge')).toEqual([])
  await trigger.click()
  await page.getByRole('dialog').getByRole('combobox').selectOption('WINC-00002')
  await page.getByRole('button', { name: 'Merge into WINC-00002' }).click()
  await expect(page).toHaveURL(/\/wolf\/incidents\/WINC-00002$/)
  expect(calls.filter((c) => c.method === 'frappe_wolf.api.incident.merge').map((c) => c.args)).toEqual([
    { source: 'WINC-00001', target: 'WINC-00002' },
  ])
})
