import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { mockApi } from './api.js'

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
