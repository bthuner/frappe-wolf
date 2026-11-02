// Time drawing the overview in Chromium, with no bench: the production build served by
// e2e/server.mjs, the Wolf API mocked in the page (e2e/api.js) with the load test's sizes, 300
// signal series and 101 hosts. Drawing is from the last API answer until the next frame.
//
//   yarn build && node e2e/server.mjs &
//   node scripts/draw-time.mjs [runs] [--empty]
//
// --empty: one series and one host, the page's drawing without its rows. WOLF_CHROMIUM points
// at a preinstalled Chromium.
import { chromium } from '@playwright/test'
import { mockApi } from '../e2e/api.js'

const args = process.argv.slice(2)
const empty = args.includes('--empty')
const runs = Number(args.find((a) => /^\d+$/.test(a)) || 15)

const NOW = Date.now()
const at = (m) => new Date(NOW - m * 60000).toISOString()
const GROUPS = ['workers', 'processes', 'redis', 'database', 'backups', 'tls', 'security', 'host']
const STATES = ['firing', 'stale', 'watched', 'unwatched']
const series = []
for (let i = 0; i < (empty ? 1 : 300); i++) {
  const state = i < 12 ? 'firing' : i < 40 ? 'stale' : i < 200 ? 'watched' : 'unwatched'
  series.push({ metric_name: `m${i % 15}`, group: GROUPS[i % 8], label: `Metric ${i % 15}`, unit: '%', host: `host-${String(i % 100).padStart(3, '0')}`,
    bench: 'frappe-bench', site: null, label_key: `k=${i}`, value: i, last_at: state === 'stale' ? at(40) : at(1), state, stale: state === 'stale',
    rules: state === 'unwatched' ? [] : [{ name: `R${i}`, rule_name: 'Rule', aggregation: 'max', operator: '>', threshold: 90, window_minutes: 5 }], incidents: [] })
}
// the server's order: state, then group
series.sort((a, b) => STATES.indexOf(a.state) - STATES.indexOf(b.state) || GROUPS.indexOf(a.group) - GROUPS.indexOf(b.group))
const counts = {}
for (const s of series) { counts[s.group] ||= {}; counts[s.group][s.state] = (counts[s.group][s.state] || 0) + 1 }
const hosts = Array.from({ length: empty ? 1 : 101 }, (_, i) => ({ host: `host-${String(i).padStart(3, '0')}`, sources: ['agent', 'metrics'],
  benches: [{ bench: 'frappe-bench', sites: [{ site: `s${i}.example.com` }, { site: `t${i}.example.com` }] }] }))
const overrides = {
  'frappe_wolf.api.dashboard.get_signals': () => ({ series, counts, missing: [], caveats: {}, email: {}, lookback_days: 7, truncated: true, total_series: 1500 }),
  'frappe_wolf.api.scope.get_inventory': () => ({ hosts, note: '' }),
}

const browser = await chromium.launch(process.env.WOLF_CHROMIUM ? { executablePath: process.env.WOLF_CHROMIUM } : {})
const out = []
for (let r = 0; r < runs; r++) {
  const context = await browser.newContext()
  const page = await context.newPage()
  await mockApi(page, overrides)
  // last API answer in the page: the route handler answers, then the page draws
  await page.addInitScript(() => {
    const f = window.fetch
    window.__last = 0
    window.fetch = async (...a) => { const res = await f(...a); const t = await res.clone().text(); window.__last = performance.now(); return new Response(t, res) }
  })
  await page.goto('http://127.0.0.1:4183/wolf/')
  await page.waitForSelector('[aria-label="Hosts, benches and sites"]')
  await page.waitForFunction(() => !document.body.innerText.includes('Loading'))
  const t = await page.evaluate(() => new Promise((ok) => requestAnimationFrame(() => setTimeout(() => ok({ last: window.__last, drawn: performance.now(), lis: document.querySelectorAll('li').length })))))
  out.push({ draw: t.drawn - t.last, lis: t.lis })
  await context.close()
}
await browser.close()
const d = out.map((o) => o.draw).sort((a, b) => a - b)
const q = (p) => d[Math.min(d.length - 1, Math.floor(p * d.length))].toFixed(0)
console.log(JSON.stringify({ runs, list_items: out[0].lis, p50: q(0.5), p95: q(0.95), max: d.at(-1).toFixed(0) }))
