// Calls to whitelisted Wolf methods with the session cookie. Errors carry the server's message,
// so a refused action says why (permission, merged incident, ...).

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

function serverMessage(body) {
  try {
    const messages = JSON.parse(body._server_messages || '[]').map((m) => JSON.parse(m).message)
    if (messages.length) return messages.join(' ').replace(/<[^>]+>/g, '')
  } catch {
    // fall through to the exception text
  }
  return (body.exception || '').split(':').slice(1).join(':').trim() || null
}

export async function call(method, args = {}, { post = false, fetchFn = window.fetch.bind(window), signal } = {}) {
  const url = `/api/method/${method}`
  const init = { headers: { Accept: 'application/json' }, signal }
  let target = url
  if (post) {
    init.method = 'POST'
    init.headers['Content-Type'] = 'application/json'
    init.headers['X-Frappe-CSRF-Token'] = window.csrf_token || ''
    init.body = JSON.stringify(args)
  } else {
    const query = new URLSearchParams(Object.entries(args).filter(([, v]) => v != null && v !== ''))
    target = query.size ? `${url}?${query}` : url
  }
  const response = await fetchFn(target, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(serverMessage(body) || `HTTP ${response.status}`, response.status)
  return body.message
}
