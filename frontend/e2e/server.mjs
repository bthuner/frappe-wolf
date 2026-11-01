// Serves the production build the way a bench does, for the browser tests (no Frappe needed):
// /assets/frappe_wolf/... from frappe_wolf/public, and /wolf/... as www/wolf.html with its Jinja
// boot block rendered empty, so the app loads its boot through the API, which the tests mock.
// Run `yarn build` first.
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../frappe_wolf')
// its own variable: a PORT left in the environment (containers, dev shells) must not move it
const port = Number(process.env.WOLF_E2E_PORT || 4183)
const TYPES = { '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.json': 'application/json', '.html': 'text/html' }  // prettier-ignore

async function page() {
  const html = await readFile(path.join(app, 'www', 'wolf.html'), 'utf8')
  return html.replace(/\{% for key in boot %\}[\s\S]*?\{% endfor %\}/, 'window["csrf_token"] = "e2e";')
}

const PUBLIC = path.join(app, 'public') + path.sep

function send(res, status, text) {
  res.writeHead(status, { 'Content-Type': 'text/plain' })
  res.end(text)
}

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost')
  try {
    if (url.pathname.startsWith('/assets/frappe_wolf/')) {
      const file = path.normalize(path.join(PUBLIC, decodeURIComponent(url.pathname.slice('/assets/frappe_wolf/'.length))))
      if (!file.startsWith(PUBLIC)) return send(res, 403, 'outside frappe_wolf/public')
      const body = await readFile(file)
      res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' })
      return res.end(body)
    }
    if (url.pathname === '/wolf' || url.pathname.startsWith('/wolf/')) {
      const html = await page()
      res.writeHead(200, { 'Content-Type': 'text/html' })
      return res.end(html)
    }
    return send(res, 404, 'not found')
  } catch (e) {
    if (e.code === 'ENOENT' && url.pathname.startsWith('/wolf')) {
      return send(res, 503, 'frappe_wolf/www/wolf.html is missing: run `yarn build` first')
    }
    if (e.code === 'ENOENT') return send(res, 404, 'not found')
    console.error(e)
    return send(res, 500, String(e))
  }
}).listen(port, '127.0.0.1', async () => {
  try {
    await page()
  } catch {
    console.error('frappe_wolf/www/wolf.html is missing: run `yarn build` before `yarn e2e`')
    process.exit(1) // fail fast instead of a webServer timeout
  }
  console.log(`wolf e2e server on http://127.0.0.1:${port}/wolf/`)
})
