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

// What the reader is told when a read fails: a refusal says what may not be seen, in words,
// not as Frappe's exception text; anything else keeps the server's reason.
export function failure(e, what) {
  if (e?.status === 403) return `You may not see ${what}.`
  return e?.message || String(e)
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
    const text = query.toString() // not query.size: missing before Safari 17 and Chrome 113
    target = text ? `${url}?${text}` : url
  }
  const response = await fetchFn(target, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(serverMessage(body) || `HTTP ${response.status}`, response.status)
  return body.message
}
