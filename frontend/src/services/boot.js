// Boot data from www/wolf.py (window.wolf, embedded in the page). The Vite dev server has no
// Jinja, so there it is fetched from frappe_wolf.api.spa.get_boot with the session cookie.

const BOOT_METHOD = '/api/method/frappe_wolf.api.spa.get_boot'

export async function loadBoot({ win = window, fetchFn = win.fetch?.bind(win) } = {}) {
  if (win.wolf) return win.wolf
  const response = await fetchFn(BOOT_METHOD, { headers: { Accept: 'application/json' } })
  if (response.status === 401 || (response.status === 403 && isGuest(win))) {
    // not signed in: the login page brings the user back here
    win.location.href = `/login?redirect-to=${encodeURIComponent(win.location.pathname + win.location.search)}`
    return null
  }
  if (response.status === 403) {
    // signed in without a Wolf role: sending them to /login would bounce straight back
    throw new Error('you need the Wolf User or Wolf Manager role to open Frappe Wolf')
  }
  if (!response.ok) throw new Error(`Wolf boot failed: HTTP ${response.status}`)
  win.wolf = (await response.json()).message
  return win.wolf
}

// Frappe sets the `user_id` cookie to "Guest" for a visitor without a session.
function isGuest(win) {
  const cookie = win.document?.cookie || ''
  const match = cookie.match(/(?:^|;\s*)user_id=([^;]*)/)
  return !match || decodeURIComponent(match[1]) === 'Guest'
}

// Log out is POST-only (frappe.handler.logout); then leave for the login page.
export async function logout({ win = window, fetchFn = win.fetch?.bind(win) } = {}) {
  await fetchFn('/api/method/logout', {
    method: 'POST',
    headers: { Accept: 'application/json', 'X-Frappe-CSRF-Token': win.csrf_token || '' },
  })
  win.location.href = '/login'
}

// Desk page that still covers a view the Wolf UI does not have yet.
export function deskFallback(boot, route) {
  const key = route.meta.fallback
  const base = boot?.desk?.fallbacks?.[key]
  if (!base) return null
  const id = route.meta.deskDoc && route.params[route.meta.deskDoc.param]
  return id ? `/app/${route.meta.deskDoc.doctype}/${encodeURIComponent(id)}` : base
}
