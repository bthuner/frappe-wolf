import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createMemoryHistory } from 'vue-router'

import { useLive } from '@/composables/useLive'
import IncidentPage from '@/pages/IncidentPage.vue'
import { makeRouter } from '@/router'
import { EVENT, createLive, socketUrl } from '@/services/live'

const RT = { available: true, namespace: '/wolf.example.com', port: 9000, dev_server: false, poll_seconds: 60 }
const BOOT = {
  time_zone: 'Europe/Paris',
  desk: { home: '/app', fallbacks: {} },
  capabilities: { incidents: { available: true }, logs: { available: false, reason: '' }, realtime: RT },
}

// a socket.io-client stand-in: records emits, lets the test fire server events
function fakeSocket() {
  const handlers = {}
  const socket = {
    connected: false,
    emitted: [],
    on: (event, fn) => ((handlers[event] ||= []).push(fn), socket),
    emit: (...args) => socket.emitted.push(args),
    fire: (event, ...args) => (handlers[event] || []).forEach((fn) => fn(...args)),
    connect() {
      socket.connected = true
      socket.fire('connect')
    },
    drop() {
      socket.connected = false
      socket.fire('disconnect')
    },
  }
  return socket
}

function hub(boot = BOOT) {
  const socket = fakeSocket()
  const connect = vi.fn(() => socket)
  const live = createLive(boot, { connect, win: { location: new URL('https://wolf.example.com/wolf/') } })
  return { live, socket, connect }
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('socketUrl', () => {
  it('uses the site origin behind nginx and the socket.io port under bench start', () => {
    const loc = new URL('https://wolf.example.com:8443/wolf/incidents')
    expect(socketUrl(RT, loc)).toBe('https://wolf.example.com:8443/wolf.example.com')
    expect(socketUrl({ ...RT, dev_server: true }, new URL('http://wolf.localhost:8000/wolf'))).toBe(
      'http://wolf.localhost:9000/wolf.example.com',
    )
  })
})

describe('createLive', () => {
  it('joins rooms on connect, rejoins after a reconnect and asks views to reconcile', async () => {
    const { live, socket, connect } = hub()
    const heard = []
    live.subscribe({ doc: ['Wolf Incident', 'WINC-1'] }, (m) => heard.push(m))
    await flushPromises()
    expect(connect).toHaveBeenCalledWith('https://wolf.example.com/wolf.example.com', expect.objectContaining({ withCredentials: true }))
    expect(live.state.value).toBe('connecting')
    socket.connect()
    expect(socket.emitted).toEqual([['doc_subscribe', 'Wolf Incident', 'WINC-1']])
    expect(live.state.value).toBe('live')
    expect(heard).toEqual([]) // the first connect is not a change

    socket.fire(EVENT, { kind: 'incident', incident: 'WINC-1' })
    socket.drop()
    expect(live.state.value).toBe('polling')
    socket.connect()
    expect(socket.emitted.at(-1)).toEqual(['doc_subscribe', 'Wolf Incident', 'WINC-1'])
    expect(heard).toEqual([{ kind: 'incident', incident: 'WINC-1' }, { kind: 'reconnect' }])
  })

  it('counts room users and leaves a room with its last one', async () => {
    const { live, socket } = hub()
    const a = live.subscribe({ doctype: 'Wolf Event' }, () => {})
    await flushPromises()
    socket.connect()
    const b = live.subscribe({ doctype: 'Wolf Event' }, () => {})
    expect(socket.emitted.filter((e) => e[0] === 'doctype_subscribe')).toHaveLength(1)
    a()
    expect(socket.emitted.some((e) => e[0] === 'doctype_unsubscribe')).toBe(false)
    b()
    expect(socket.emitted.at(-1)).toEqual(['doctype_unsubscribe', 'Wolf Event'])
  })

  it('polls when the server offers no socket', () => {
    const { live, connect } = hub({ capabilities: { realtime: { available: false, reason: 'no socket.io' } } })
    live.subscribe({ doctype: 'Wolf Event' }, () => {})
    expect(connect).not.toHaveBeenCalled()
    expect(live.state.value).toBe('polling')
  })
})

function harness(live, refresh, options = {}) {
  let api
  const Comp = defineComponent({
    setup() {
      api = useLive(live, { room: () => ({ doctype: 'Wolf Incident' }), matches: (m) => m.kind === 'incidents', refresh, ...options })
      return () => h('div')
    },
  })
  const wrapper = mount(Comp)
  return { wrapper, api: () => api }
}

describe('useLive', () => {
  it('coalesces a burst of messages into one refresh and ignores other kinds', async () => {
    vi.useFakeTimers()
    const { live, socket } = hub()
    const refresh = vi.fn(async () => true)
    harness(live, refresh)
    await flushPromises()
    socket.connect()
    for (let i = 0; i < 5; i++) socket.fire(EVENT, { kind: 'incidents' })
    socket.fire(EVENT, { kind: 'events' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledWith('message')
  })

  it('holds changes while paused and applies them on resume', async () => {
    vi.useFakeTimers()
    const { live, socket } = hub()
    const refresh = vi.fn(async () => true)
    const { api } = harness(live, refresh)
    await flushPromises()
    socket.connect()
    api().toggle()
    socket.fire(EVENT, { kind: 'incidents' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(refresh).not.toHaveBeenCalled()
    expect(api().pending.value).toBe(true)
    api().toggle()
    await flushPromises()
    expect(refresh).toHaveBeenCalledWith('manual')
    expect(api().pending.value).toBe(false)
  })

  it('never claims a change after a poll', async () => {
    vi.useFakeTimers()
    const { live } = hub({ capabilities: { realtime: { available: false } } })
    const refresh = vi.fn(async (reason) => reason === 'manual')
    const { api } = harness(live, refresh)
    await vi.advanceTimersByTimeAsync(61000)
    expect(refresh).toHaveBeenCalledWith('poll')
    expect(api().pending.value).toBe(false) // nothing is known to have changed
  })

  it('leaves an announced change pending when the view cannot apply it', async () => {
    vi.useFakeTimers()
    const { live, socket } = hub()
    const refresh = vi.fn(async (reason) => reason === 'manual')
    const { api } = harness(live, refresh)
    await flushPromises()
    socket.connect()
    socket.fire(EVENT, { kind: 'incidents' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(api().pending.value).toBe(true)
    await api().apply()
    expect(refresh).toHaveBeenLastCalledWith('manual')
    expect(api().pending.value).toBe(false)
  })

  it('does not poll while live unless asked to', async () => {
    vi.useFakeTimers()
    const { live, socket } = hub()
    const refresh = vi.fn(async () => true)
    harness(live, refresh)
    const always = vi.fn(async () => true)
    harness(live, always, { pollAlways: true })
    await flushPromises()
    socket.connect()
    await vi.advanceTimersByTimeAsync(61000)
    expect(refresh).not.toHaveBeenCalled()
    expect(always).toHaveBeenCalledWith('poll')
  })

  it('leaves its rooms when the view goes away', async () => {
    const { live, socket } = hub()
    const { wrapper } = harness(live, async () => true)
    await flushPromises()
    socket.connect()
    wrapper.unmount()
    expect(socket.emitted.at(-1)).toEqual(['doctype_unsubscribe', 'Wolf Incident'])
  })

  it('does nothing without a hub', () => {
    const refresh = vi.fn()
    const { api } = harness(null, refresh)
    expect(api().state.value).toBe('off')
    expect(refresh).not.toHaveBeenCalled()
  })
})

describe('IncidentPage live', () => {
  const INCIDENT = {
    name: 'WINC-00001', title: '502 spike', status: 'Open', severity: 'critical', scope: { site: 'a.example.com' },
    started_at: '2026-10-30T00:48:00Z', acknowledged: { at: null }, resolved: { at: null }, assigned_to: null,
    alert_rule: null, deployment: null, silenced: false, merged_into: null, event_records: 1, event_count: 1,
    can: { comment: true, acknowledge: true, resolve: true, assign_to_me: true, workflow: false },
  }
  const row = (id, at) => ({ id, kind: 'event', family: 'evidence', relation: 'linked', at, severity: 'critical',
    summary: `502 ${id}`, scope: {}, count: 1, details: {} })  // prettier-ignore

  it('updates the header and offers new rows without scrolling to them', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
    let rows = [row('event:E1', '2026-10-30T00:48:00Z')]
    let status = 'Open'
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url) => {
        const method = url.split('/api/method/')[1].split('?')[0]
        const message = method.endsWith('get_incident')
          ? { ...INCIDENT, status }
          : { rows, paging: { has_more: false }, totals: {}, truncated: {} }
        return new Response(JSON.stringify({ message }), { status: 200 })
      }),
    )
    const { live, socket } = hub()
    const router = makeRouter(createMemoryHistory('/wolf'), BOOT)
    await router.push('/incidents/WINC-00001')
    await router.isReady()
    const page = mount(IncidentPage, { global: { plugins: [router], provide: { boot: BOOT, live } }, attachTo: document.body })
    await flushPromises()
    socket.connect()
    await flushPromises()
    expect(socket.emitted).toContainEqual(['doc_subscribe', 'Wolf Incident', 'WINC-00001'])
    expect(page.text()).toContain('Live')

    rows = [...rows, row('event:E2', '2026-10-30T00:49:00Z')]
    status = 'Acknowledged'
    socket.fire(EVENT, { kind: 'incident', incident: 'WINC-00002' }) // another incident: ignored
    socket.fire(EVENT, { kind: 'incident', incident: 'WINC-00001' })
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(page.text()).toContain('Acknowledged')
    expect(page.text()).toContain('502 event:E2')
    const reveal = page.findAll('button').find((b) => b.text() === '1 new row: show')
    expect(reveal).toBeTruthy()
    await reveal.trigger('click')
    await flushPromises()
    expect(page.findAll('button').some((b) => b.text().includes('new row'))).toBe(false)
  })
})
