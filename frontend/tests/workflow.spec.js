import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MergeDialog from '@/components/MergeDialog.vue'
import SplitDialog from '@/components/SplitDialog.vue'

// The merge and split dialogs: they show what the server says would move, post only on
// confirmation, and keep the reader's choice when the server refuses.

const card = (name, extra = {}) => ({
  name, title: `incident ${name}`, status: 'Open', severity: 'warning',
  scope: { site: 'a.example.com', bench: 'frappe-bench', host: 'h1' }, started_at: '2026-10-30T00:48:00Z', ...extra,
}) // prettier-ignore

function server(handlers) {
  const calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, init) => {
      const method = url.split('/api/method/')[1].split('?')[0]
      const args = init?.body ? JSON.parse(init.body) : Object.fromEntries(new URL(url, 'http://x').searchParams)
      calls.push({ method, args })
      const result = await handlers[method](args)
      if (result instanceof Response) return result
      return new Response(JSON.stringify({ message: result }), { status: 200 })
    }),
  )
  return calls
}

const refuse = (message) =>
  new Response(JSON.stringify({ exception: `frappe.exceptions.ValidationError: ${message}` }), { status: 417 })

async function mounted(component, props) {
  const wrapper = mount(component, { props: { open: false, timeZone: 'Europe/Paris', ...props }, attachTo: document.body })
  await wrapper.setProps({ open: true })
  await flushPromises()
  return wrapper
}

const body = () => document.body.textContent.replace(/\s+/g, ' ')
const button = (label) => [...document.body.querySelectorAll('button')].find((b) => b.textContent.trim() === label)

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('MergeDialog', () => {
  const preview = (target) => ({
    source: card('WINC-1'),
    candidates: [card('WINC-2'), card('WINC-3', { severity: 'critical' })],
    more_candidates: false,
    preview: target && {
      error: null, target: card(target), events: 3, comments: 1,
      target_after: { severity: 'critical', started_at: '2026-10-30T00:40:00Z', status: 'Open' },
      source_after: { status: 'Resolved', merged_into: target },
    },
  }) // prettier-ignore

  it('says what moves and what both incidents become before merging, then merges once', async () => {
    const calls = server({
      'frappe_wolf.api.incident_view.merge_preview': (args) => preview(args.target),
      'frappe_wolf.api.incident.merge': () => ({ target: 'WINC-2', events: 3, comments: 1 }),
    })
    const dialog = await mounted(MergeDialog, { incident: 'WINC-1' })
    expect(button('Merge').disabled).toBe(true) // nothing picked, nothing to confirm
    const select = document.body.querySelector('[data-testid="merge-target"]')
    select.value = 'WINC-2'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(body()).toContain('Moves 3 events and 1 comment from WINC-1 to WINC-2')
    expect(body()).toContain('WINC-2 stays Open')
    expect(body()).toContain('WINC-1 becomes Resolved, merged into WINC-2')
    expect(calls.filter((c) => c.method.endsWith('.merge'))).toEqual([])
    button('Merge into WINC-2').click()
    await flushPromises()
    expect(calls.filter((c) => c.method.endsWith('.merge')).map((c) => c.args)).toEqual([{ source: 'WINC-1', target: 'WINC-2' }])
    expect(dialog.emitted('merged')).toEqual([['WINC-2']])
  })

  it("keeps the choice and shows the server's reason when it refuses", async () => {
    server({
      'frappe_wolf.api.incident_view.merge_preview': (args) => preview(args.target),
      'frappe_wolf.api.incident.merge': () => refuse('WINC-2 was merged into WINC-9: merge into that incident instead.'),
    })
    const dialog = await mounted(MergeDialog, { incident: 'WINC-1' })
    const select = document.body.querySelector('[data-testid="merge-target"]')
    select.value = 'WINC-2'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    button('Merge into WINC-2').click()
    await flushPromises()
    expect(body()).toContain('merge into that incident instead')
    expect(select.value).toBe('WINC-2')
    expect(dialog.emitted('merged')).toBeUndefined()
  })

  it('cannot confirm a merge the server says is impossible', async () => {
    server({
      'frappe_wolf.api.incident_view.merge_preview': (args) => ({
        ...preview(args.target),
        preview: args.target && { ...preview(args.target).preview, error: 'You may not change WINC-3.' },
      }),
    })
    await mounted(MergeDialog, { incident: 'WINC-1' })
    const select = document.body.querySelector('[data-testid="merge-target"]')
    select.value = 'WINC-3'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(body()).toContain('You may not change WINC-3.')
    expect(button('Merge into WINC-3').disabled).toBe(true)
  })
})

describe('SplitDialog', () => {
  const EVENTS = [
    { name: 'E1', title: '502 on GET /api', severity: 'critical', at: '2026-10-30T00:48:00Z', count: 14, scope: {} },
    { name: 'E2', title: 'Job failed', severity: 'warning', at: '2026-10-30T01:10:00Z', count: 1, scope: {} },
  ]
  const answer = (selected) => ({
    incident: card('WINC-1'), merged_into: null, events: EVENTS, more_events: false,
    preview: selected && {
      error: null, selection: selected, moved: selected.length, kept: EVENTS.length - selected.length,
      new_incident: { title: 'Job failed', severity: 'warning', started_at: '2026-10-30T01:10:00Z', status: 'Open' },
    },
  }) // prettier-ignore

  it('previews the new incident from the ticked events and splits them with the title', async () => {
    const calls = server({
      'frappe_wolf.api.incident_view.split_preview': (args) => answer(args.events && JSON.parse(args.events)),
      'frappe_wolf.api.incident.split': () => 'WINC-7',
    })
    const dialog = await mounted(SplitDialog, { incident: 'WINC-1' })
    expect(body()).toContain('14 reported') // a deduplicated record, shown as such
    const boxes = document.body.querySelectorAll('input[type="checkbox"]')
    boxes[1].click()
    await flushPromises()
    expect(body()).toContain('Moves 1 event to a new incident; 1 event stays in WINC-1')
    const title = document.body.querySelector('input[aria-label="Title of the new incident"]')
    title.value = 'Nightly job'
    title.dispatchEvent(new Event('input'))
    await flushPromises()
    button('Split 1 event').click()
    await flushPromises()
    const split = calls.filter((c) => c.method.endsWith('.split'))
    expect(split.map((c) => c.args)).toEqual([{ incident: 'WINC-1', events: '["E2"]', title: 'Nightly job' }])
    expect(dialog.emitted('split')).toEqual([['WINC-7']])
  })

  it('never offers to confirm a preview made for another selection', async () => {
    let fail = false
    server({
      'frappe_wolf.api.incident_view.split_preview': (args) => (fail ? new Response('{}', { status: 502 }) : answer(args.events && JSON.parse(args.events))),
    })
    await mounted(SplitDialog, { incident: 'WINC-1' })
    const boxes = document.body.querySelectorAll('input[type="checkbox"]')
    boxes[1].click()
    await flushPromises()
    expect(button('Split 1 event').disabled).toBe(false)
    fail = true // the next preview fails: the one shown is about [E2] only
    boxes[0].click()
    await flushPromises()
    expect(body()).not.toContain('Moves 1 event')
    expect(button('Split 2 events').disabled).toBe(true)
  })

  it('keeps the selection and title when the server refuses', async () => {
    server({
      'frappe_wolf.api.incident_view.split_preview': (args) => answer(args.events && JSON.parse(args.events)),
      'frappe_wolf.api.incident.split': () => refuse('Not linked to this incident: E2.'),
    })
    const dialog = await mounted(SplitDialog, { incident: 'WINC-1' })
    document.body.querySelectorAll('input[type="checkbox"]')[1].click()
    await flushPromises()
    button('Split 1 event').click()
    await flushPromises()
    expect(body()).toContain('Not linked to this incident: E2.')
    expect(document.body.querySelectorAll('input[type="checkbox"]')[1].checked).toBe(true)
    expect(dialog.emitted('split')).toBeUndefined()
  })
})
