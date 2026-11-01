import { onUnmounted, ref, watch } from 'vue'

// Keep a view current with the live hub (services/live.js).
//
// `room()` names the room or rooms to join ({doc: [doctype, name]}, {doctype}, a list of them,
// or null) and may depend on reactive state; `matches(message)` picks the messages for this view.
// `refresh(reason)` fetches again; reason is 'message' (the server said this view changed),
// 'connect' (the socket just joined: anything since the page's first fetch is unknown),
// 'reconnect' (it may have changed while offline), 'poll' (no socket: a timer) or 'manual' (the
// reader asked). It may return false when applying would move what the reader is looking at
// (pages loaded, a list reshuffled). The change then waits as `pending`: `pendingReason` says
// 'message' (a change is known) or 'reconnect' (one may have been missed). After a poll or the
// first connect nothing is claimed.
//
// Every refresh goes through one queue, the reader's included: coalesced (one per `debounceMs`)
// and never two at once. While paused, messages only mark the view pending. Polling skips
// hidden tabs; `pollAlways` keeps it on while live, for views that also show data no message
// announces (metrics arrive every 30 s); `polls: false` turns it off for views that would not
// apply it anyway, so they do not claim to check.
export function useLive(
  live,
  { room = () => null, matches = () => true, refresh, debounceMs = 1000, pollAlways = false, polls = true, win = window },
) {
  const paused = ref(false)
  const pending = ref(false)
  const pendingReason = ref(null)
  const state = live ? live.state : ref('off')
  let timer = null
  let poller = null
  let running = false
  let queued = null // the strongest reason waiting for the timer
  let again = null // a reason that arrived during a fetch
  let waiters = [] // apply() calls waiting for their manual refresh
  let undos = []
  const RANK = { poll: 0, connect: 1, reconnect: 2, message: 3, manual: 4 }

  function stronger(a, b) {
    if (!a) return b
    return RANK[b] > RANK[a] ? b : a
  }

  function markPending(reason) {
    if (reason !== 'message' && reason !== 'reconnect') return // nothing is known to have changed
    pending.value = true
    pendingReason.value = stronger(pendingReason.value, reason)
  }

  function settle() {
    const done = waiters
    waiters = []
    for (const resolve of done) resolve()
  }

  async function run() {
    timer = null
    const reason = queued
    queued = null
    if (!reason) return settle()
    if (paused.value && reason !== 'manual') {
      markPending(reason)
      return
    }
    if (running) {
      again = stronger(again, reason) // one more fetch once this one is done
      return
    }
    running = true
    const manual = reason === 'manual'
    try {
      const applied = await refresh(reason)
      if (applied === false) markPending(reason)
      else {
        pending.value = false
        pendingReason.value = null
      }
    } finally {
      running = false
      if (manual) settle()
      if (again) {
        const next = again
        again = null
        schedule(next, next === 'manual' ? 0 : debounceMs)
      }
    }
  }

  function schedule(reason, delay = debounceMs) {
    queued = stronger(queued, reason)
    if (timer && delay > 0) return
    if (timer) win.clearTimeout(timer)
    timer = win.setTimeout(run, delay)
  }

  function onMessage(message) {
    if (message.kind === 'reconnect' || message.kind === 'connect') schedule(message.kind)
    else if (matches(message)) schedule('message')
  }

  function poll() {
    if ((state.value === 'live' && !pollAlways) || paused.value || win.document?.hidden) return
    schedule('poll')
  }

  if (live) {
    watch(
      room,
      (next) => {
        for (const undo of undos) undo()
        const list = next == null ? [null] : Array.isArray(next) ? next : [next]
        // one listener per room, so leaving one room never drops the others' listener
        undos = list.map((r) => live.subscribe(r, (message) => onMessage(message)))
      },
      { immediate: true, deep: true },
    )
    if (polls) poller = win.setInterval(poll, live.pollSeconds * 1000)
  }

  // the reader asks (Refresh, a pending button, Resume): through the queue, at once; resolves
  // once that refresh is done. Without a hub there is no queue: refresh directly.
  function apply() {
    paused.value = false
    if (!live) return Promise.resolve(refresh('manual'))
    return new Promise((resolve) => {
      waiters.push(resolve)
      schedule('manual', 0)
    })
  }

  function toggle() {
    paused.value = !paused.value
    if (!paused.value && pending.value) apply()
  }

  onUnmounted(() => {
    for (const undo of undos) undo()
    if (timer) win.clearTimeout(timer)
    if (poller) win.clearInterval(poller)
    settle()
  })

  return { state, paused, pending, pendingReason, polls: !!live && polls, apply, toggle }
}
