// Time a Wolf UI page in Chromium against a running bench: from navigation start until every
// panel has its data (no "Loading …" note left) and the page's reads have answered.
//
//   node scripts/page-time.mjs --url http://127.0.0.1:8099 --site load.localhost \
//     --password admin [--path /wolf] [--runs 10] [--reads 6]
//
// --reads: how many Wolf reads the page sends (the overview: six). Ready waits for all of them.
// --pause: seconds between two visits, to time pages opened after a quiet spell (a cache that
//   expires, a value read again in the background). --series cold or warm runs one series only.
//
// Two series:
// - cold: a new browser context for each run, so the app's scripts and styles are fetched again
//   (the first visit of the day);
// - warm: the same tab navigating again, assets cached (moving around the app).
// Reads are timed in the browser (Resource Timing): queueing behind the browser's six connections
// per host, the network and the server together. Measures a bench, not a user's network.
import { chromium } from '@playwright/test'

const opts = { url: 'http://127.0.0.1:8099', site: 'localhost', user: 'Administrator', password: 'admin', path: '/wolf', runs: 10, reads: 6, pause: 0, series: 'cold,warm' }
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, '')
  if (!(key in opts)) throw new Error(`unknown option --${key}`)
  opts[key] = ['runs', 'reads', 'pause'].includes(key) ? Number(process.argv[i + 1]) : process.argv[i + 1]
}

const target = new URL(opts.url)
const base = `${target.protocol}//${opts.site}:${target.port || 80}`
// the site is chosen by Host header: resolve its name to the bench without touching /etc/hosts
const browser = await chromium.launch({ args: [`--host-resolver-rules=MAP ${opts.site} ${target.hostname}`] })

const READ = /\/api\/method\/(frappe_wolf\.api\.[\w.]+)/

async function login() {
  // from inside the page: Node's request client would not use the browser's host mapping
  const context = await browser.newContext({ baseURL: base })
  const page = await context.newPage()
  await page.goto('/api/method/ping')
  const status = await page.evaluate(async ({ usr, pwd }) => {
    const res = await fetch('/api/method/login', { method: 'POST', body: new URLSearchParams({ usr, pwd }) })
    return res.status
  }, { usr: opts.user, pwd: opts.password })
  if (status !== 200) throw new Error(`login failed: HTTP ${status}`)
  const state = await context.storageState()
  await context.close()
  return state
}

async function measure(page) {
  const failures = []
  const started = new Set()
  const done = new Set()
  let allAnswered
  const answered = new Promise((resolve) => (allAnswered = resolve))
  const settle = (r) => {
    if (!started.has(r)) return
    done.add(r)
    if (done.size >= opts.reads && done.size === started.size) allAnswered()
  }
  const onRequest = (r) => READ.test(r.url()) && started.add(r)
  const onResponse = (r) => {
    if (READ.test(r.url()) && r.status() >= 400) failures.push(`${r.url().match(READ)[1]}: HTTP ${r.status()}`)
  }
  const onFailed = (r) => {
    if (READ.test(r.url())) failures.push(`${r.url().match(READ)[1]}: ${r.failure()?.errorText}`)
    settle(r)
  }
  page.on('request', onRequest)
  page.on('response', onResponse)
  page.on('requestfinished', settle)
  page.on('requestfailed', onFailed)
  await page.goto(opts.path, { waitUntil: 'commit' })
  await Promise.race([answered, page.waitForTimeout(120_000).then(() => { throw new Error(`${done.size} of ${opts.reads} reads answered in 120 s`) })])
  // then the frame where no panel says it is loading any more: the data is drawn
  const ready = await page.waitForFunction(
    () => {
      const loadingNotes = [...document.querySelectorAll('[role=status]')].filter((n) => n.textContent.startsWith('Loading'))
      return loadingNotes.length === 0 ? performance.now() : false
    },
    null,
    { polling: 'raf', timeout: 30_000 },
  )
  const at = await ready.jsonValue()
  const timing = await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0]
    const reads = performance.getEntriesByType('resource')
      .filter((e) => e.name.includes('/api/method/frappe_wolf.api.'))
      .map((e) => ({ name: e.name.match(/frappe_wolf\.api\.([\w.]+)/)[1], start: e.startTime, end: e.responseEnd }))
    return { domContentLoaded: nav?.domContentLoadedEventEnd, reads }
  })
  for (const [event, fn] of [['request', onRequest], ['response', onResponse], ['requestfinished', settle], ['requestfailed', onFailed]]) page.off(event, fn)
  if (timing.reads.length !== started.size) failures.push(`${started.size} reads sent, ${timing.reads.length} timed`)
  return { ready: at, ...timing, failures }
}

function stats(values) {
  const s = [...values].sort((a, b) => a - b)
  const pick = (q) => s[Math.min(s.length - 1, Math.ceil(q * s.length) - 1)]
  return { n: s.length, p50: pick(0.5), p95: pick(0.95), max: s[s.length - 1] }
}

function report(label, runs) {
  const fmt = (v) => (v === undefined ? '' : v.toFixed(0))
  const row = (name, values) => {
    const { n, p50, p95, max } = stats(values)
    console.log(`| ${label} | ${name} | ${n} | ${fmt(p50)} | ${fmt(p95)} | ${fmt(max)} |`)
  }
  row('page ready (navigation start → all panels drawn)', runs.map((r) => r.ready))
  row('app shell (DOMContentLoaded)', runs.map((r) => r.domContentLoaded))
  row('first read sent (app started, session checked)', runs.map((r) => Math.min(...r.reads.map((x) => x.start))))
  row('reads window (first read sent → last answered)', runs.map((r) => Math.max(...r.reads.map((x) => x.end)) - Math.min(...r.reads.map((x) => x.start))))
  row('drawing (last read answered → panels drawn)', runs.map((r) => r.ready - Math.max(...r.reads.map((x) => x.end))))
  const names = [...new Set(runs.flatMap((r) => r.reads.map((x) => x.name)))].sort()
  for (const name of names) {
    row(`  ${name}`, runs.flatMap((r) => r.reads.filter((x) => x.name === name).map((x) => x.end - x.start)))
  }
}

const state = await login()
const wanted = new Set(opts.series.split(','))
const series = { cold: [], warm: [] }
const pause = () => new Promise((resolve) => setTimeout(resolve, opts.pause * 1000))
// one throwaway visit: the first request after a restart pays for imports, not for the page
{
  const context = await browser.newContext({ baseURL: base, storageState: state })
  await measure(await context.newPage())
  await context.close()
}
if (wanted.has('cold')) {
  for (let i = 0; i < opts.runs; i++) {
    await pause()
    const context = await browser.newContext({ baseURL: base, storageState: state })
    series.cold.push(await measure(await context.newPage()))
    await context.close()
  }
}
if (wanted.has('warm')) {
  const context = await browser.newContext({ baseURL: base, storageState: state })
  const page = await context.newPage()
  await measure(page)
  for (let i = 0; i < opts.runs; i++) {
    await pause()
    series.warm.push(await measure(page))
  }
  await context.close()
}
const version = browser.version()
await browser.close()

const failures = [...series.cold, ...series.warm].flatMap((r) => r.failures)
console.log(`${opts.path} on ${opts.site}, Chromium ${version}, ${opts.runs} runs per series, ${opts.pause} s between visits, in ms`)
console.log('| series | measure | n | p50 | p95 | max |')
console.log('|---|---|---|---|---|---|')
for (const name of ['cold', 'warm']) if (series[name].length) report(name, series[name])
if (failures.length) {
  console.log(`\n${failures.length} failed read(s): ${[...new Set(failures)].join('; ')}`)
  process.exitCode = 1
}
