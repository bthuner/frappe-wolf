// Serves the production build the way a bench does, for the browser tests (no Frappe needed):
// /assets/frappe_wolf/... from frappe_wolf/public, and /wolf/... as www/wolf.html with its Jinja
// boot block rendered empty, so the app loads its boot through the API, which the tests mock.
// Run `yarn build` first.
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../frappe_wolf')
const port = Number(process.env.PORT || 4173)
const TYPES = { '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.json': 'application/json', '.html': 'text/html' }  // prettier-ignore

async function page() {
  const html = await readFile(path.join(app, 'www', 'wolf.html'), 'utf8')
  return html.replace(/\{% for key in boot %\}[\s\S]*?\{% endfor %\}/, 'window["csrf_token"] = "e2e";')
}

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost')
  try {
    if (url.pathname.startsWith('/assets/frappe_wolf/')) {
      const file = path.normalize(path.join(app, 'public', url.pathname.slice('/assets/frappe_wolf/'.length)))
      if (!file.startsWith(path.join(app, 'public'))) throw new Error('outside')
      const body = await readFile(file)
      res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' })
      return res.end(body)
    }
    if (url.pathname === '/wolf' || url.pathname.startsWith('/wolf/')) {
      res.writeHead(200, { 'Content-Type': 'text/html' })
      return res.end(await page())
    }
  } catch {
    // fall through to 404
  }
  res.writeHead(404, { 'Content-Type': 'text/plain' })
  res.end('not found')
}).listen(port, '127.0.0.1', () => console.log(`wolf e2e server on http://127.0.0.1:${port}/wolf/`))
