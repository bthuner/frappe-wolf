import { onUnmounted, ref, watch } from 'vue'

// Keep a view current with the live hub (services/live.js).
//
// `room()` names the room or rooms to join ({doc: [doctype, name]}, {doctype}, a list of them,
// or null) and may depend on reactive state; `matches(message)` picks the messages for this view. `refresh(reason)`
// fetches again; reason is 'message' (the server said this view changed), 'reconnect' (it may
// have, while offline), 'poll' (no socket: a timer) or 'manual' (the reader asked). It may return
// false when applying would move what the reader is looking at (pages loaded, a list
// reshuffled). After a message the change then waits as `pending` for the reader; after a
// reconnect or a poll nothing is claimed, since nothing is known to have changed.
//
// Requests are coalesced (one refresh per `debounceMs`, never two at once). While paused,
// messages only mark the view pending. Polling skips hidden tabs; `pollAlways` keeps it on
// while live, for views that also show data no message announces (metrics arrive every 30 s).
export function useLive(
  live,
  { room = () => null, matches = () => true, refresh, debounceMs = 1000, pollAlways = false, win = window },
) {
  const paused = ref(false)
  const pending = ref(false)
  const state = live ? live.state : ref('off')
  let timer = null
  let poller = null
  let running = false
  let queued = null // the strongest reason waiting for the timer
  let again = null // a reason that arrived during a fetch
  let undos = []
  const RANK = { poll: 0, reconnect: 1, message: 2 }

  function stronger(a, b) {
    if (!a) return b
    return RANK[b] > RANK[a] ? b : a
  }

  async function run() {
    timer = null
    const reason = queued
    queued = null
    if (paused.value) {
      if (reason === 'message') pending.value = true
      return
    }
    if (running) {
      again = stronger(again, reason) // one more fetch once this one is done
      return
    }
    running = true
    try {
      const applied = await refresh(reason)
      if (applied === false && reason === 'message') pending.value = true
      else if (applied !== false) pending.value = false
    } finally {
      running = false
      if (again) {
        const next = again
        again = null
        schedule(next)
      }
    }
  }

  function schedule(reason) {
    queued = stronger(queued, reason)
    if (timer) return
    timer = win.setTimeout(run, debounceMs)
  }

  function onMessage(message) {
    if (message.kind === 'reconnect') schedule('reconnect')
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
    poller = win.setInterval(poll, live.pollSeconds * 1000)
  }

  // the reader applies a waiting change, or resumes: fetch now
  async function apply() {
    paused.value = false
    if (timer) win.clearTimeout(timer)
    timer = null
    queued = null
    pending.value = false
    await refresh('manual')
  }

  function toggle() {
    paused.value = !paused.value
    if (!paused.value && pending.value) apply()
  }

  onUnmounted(() => {
    for (const undo of undos) undo()
    if (timer) win.clearTimeout(timer)
    if (poller) win.clearInterval(poller)
  })

  return { state, paused, pending, apply, toggle }
}
