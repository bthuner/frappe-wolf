// Live updates (frappe_wolf/wolf/live.py). The server only says *that* something changed, in
// rooms Frappe guards with the reader's permissions (doc_subscribe checks read permission on the
// incident, doctype_subscribe on the DocType); views then fetch again through the APIs.
//
// One socket for the whole app. Rooms are reference-counted and joined again on every
// (re)connect, because the server drops a socket's rooms when it disconnects. After a reconnect
// every subscriber reconciles once: messages sent while offline are lost. Without a socket
// (capability off, connection refused, Frappe's socket.io not running) views poll instead.

import { ref } from 'vue'

export const EVENT = 'wolf_invalidate'

// Behind nginx, socket.io is proxied on the site's own origin; under `bench start` it listens on
// its own port, as in Frappe Desk's socketio_client.
export function socketUrl(rt, loc = window.location) {
  if (!rt.dev_server) return `${loc.origin}${rt.namespace}`
  return `${loc.protocol}//${loc.hostname}:${rt.port}${rt.namespace}`
}

function roomKey(room) {
  return room.doc ? `doc:${room.doc[0]}/${room.doc[1]}` : `doctype:${room.doctype}`
}

function join(socket, room) {
  if (room.doc) socket.emit('doc_subscribe', room.doc[0], room.doc[1])
  else socket.emit('doctype_subscribe', room.doctype)
}

function leave(socket, room) {
  if (room.doc) socket.emit('doc_unsubscribe', room.doc[0], room.doc[1])
  else socket.emit('doctype_unsubscribe', room.doctype)
}

// `connect(url, options)` is socket.io-client's `io`, injected so tests need no server.
export function createLive(boot, { connect = null, win = window } = {}) {
  const rt = boot?.capabilities?.realtime || {}
  // connecting, live, or polling: no socket, views refresh on a timer
  const state = ref(rt.available ? 'connecting' : 'polling')
  const pollSeconds = rt.poll_seconds || 60
  const rooms = new Map() // key -> { room, count }
  const listeners = new Set()
  let socket = null
  let wasConnected = false
  let starting = null

  function emitAll(message) {
    for (const listener of [...listeners]) listener(message)
  }

  async function start() {
    if (!rt.available || socket || starting) return starting
    starting = (async () => {
      try {
        const io = connect || (await import('socket.io-client')).io
        socket = io(socketUrl(rt, win.location), { withCredentials: true, reconnectionAttempts: 10 })
      } catch {
        state.value = 'polling'
        return
      }
      socket.on('connect', () => {
        for (const { room } of rooms.values()) join(socket, room)
        state.value = 'live'
        // what changed while offline is unknown; on the first connect, what changed between the
        // page's first fetch and joining its rooms is unknown too
        emitAll({ kind: wasConnected ? 'reconnect' : 'connect' })
        wasConnected = true
      })
      socket.on('disconnect', () => {
        state.value = 'polling'
      })
      socket.on('connect_error', () => {
        state.value = 'polling'
      })
      socket.io?.on?.('reconnect_failed', () => {
        state.value = 'polling'
      })
      socket.on(EVENT, (message) => emitAll(message || {}))
    })()
    return starting
  }

  // Join `room` ({doc: [doctype, name]} or {doctype}) and hear every message; returns the undo.
  function subscribe(room, listener) {
    listeners.add(listener)
    const key = room ? roomKey(room) : null
    if (key) {
      const entry = rooms.get(key)
      if (entry) entry.count += 1
      else {
        rooms.set(key, { room, count: 1 })
        if (socket?.connected) join(socket, room)
      }
    }
    start()
    return () => {
      listeners.delete(listener)
      if (!key) return
      const entry = rooms.get(key)
      if (entry && --entry.count === 0) {
        rooms.delete(key)
        if (socket?.connected) leave(socket, room)
      }
    }
  }

  return { state, pollSeconds, subscribe, start }
}
