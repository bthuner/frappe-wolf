import { afterEach, describe, expect, it, vi } from 'vitest'

import { focusPageTitle, installFocusGuard, replacement } from '@/services/focus'

// jsdom lays nothing out and keeps focus on a control that becomes disabled: the tests say
// "everything is visible" and blur the way Chromium does (the browser tests check Chromium).
const visible = () => true
const flush = () => new Promise((r) => setTimeout(r, 0))
let uninstall = null

function page(html) {
  document.body.innerHTML = html
  uninstall = installFocusGuard(document, { visible })
}

afterEach(() => {
  uninstall?.()
  uninstall = null
  document.body.innerHTML = ''
})

describe('focus guard', () => {
  it('gives focus back to a button once it is enabled again', async () => {
    page('<main><button id="refresh">Refresh</button><button>Other</button></main>')
    const button = document.getElementById('refresh')
    button.focus()
    // Chromium drops focus from a control that becomes disabled (jsdom cannot blur it after)
    button.blur()
    button.disabled = true
    await flush()
    expect(document.activeElement).toBe(document.body)
    button.disabled = false
    await flush()
    expect(document.activeElement).toBe(button)
  })

  it('moves focus to what is left in the same group when a button disappears', async () => {
    page('<main><div class="actions"><button id="ack">Acknowledge</button><button id="resolve">Resolve</button></div></main>')
    document.getElementById('ack').focus()
    document.getElementById('ack').remove()
    await flush()
    expect(document.activeElement.id).toBe('resolve')
  })

  it('prefers the re-rendered copy of the same control', async () => {
    page('<main><section><button>Other</button><span id="slot"><button>Approve…</button></span></section></main>')
    document.querySelector('#slot button').focus()
    document.getElementById('slot').innerHTML = '<button id="copy">Approve…</button>'
    await flush()
    expect(document.activeElement.id).toBe('copy')
  })

  it('never takes focus back from where the user or the page put it', async () => {
    page('<main><button id="a">Comment</button><textarea id="box"></textarea></main>')
    const button = document.getElementById('a')
    button.focus()
    button.disabled = true
    document.getElementById('box').focus() // the page moved focus on purpose
    button.disabled = false
    await flush()
    expect(document.activeElement.id).toBe('box')
  })

  it('leaves focus alone after a click on empty space', async () => {
    page('<main><button id="a">Refresh</button><p id="text">text</p></main>')
    const button = document.getElementById('a')
    button.focus()
    document.getElementById('text').dispatchEvent(new Event('pointerdown', { bubbles: true }))
    button.blur()
    button.disabled = true
    await flush()
    button.disabled = false
    await flush()
    expect(document.activeElement).toBe(document.body)
  })

  it('stops waiting for a control that stays disabled', async () => {
    vi.useFakeTimers()
    try {
      document.body.innerHTML = '<main><button id="a">Load more</button></main>'
      uninstall = installFocusGuard(document, { visible, giveUpMs: 1000 })
      const button = document.getElementById('a')
      button.focus()
      button.blur()
      button.disabled = true
      await vi.advanceTimersByTimeAsync(1500)
      button.disabled = false
      await vi.advanceTimersByTimeAsync(10)
      expect(document.activeElement).toBe(document.body)
    } finally {
      vi.useRealTimers()
    }
  })

  it('skips hidden and inert candidates', () => {
    document.body.innerHTML = '<div id="g"><button aria-hidden="true">x</button><div inert><button>y</button></div><a href="#z">z</a></div>'
    const g = document.getElementById('g')
    expect(replacement({ tag: 'BUTTON', label: 'gone', ancestors: [g] }, visible).textContent).toBe('z')
    expect(replacement({ tag: 'BUTTON', label: 'gone', ancestors: [g] }, () => false)).toBe(null)
  })
})

describe('focusPageTitle', () => {
  it('waits for the page title to render, then focuses it', async () => {
    let tick = 0
    const wait = async () => {
      if (++tick === 2) document.body.innerHTML = '<h1 data-wolf-page-title tabindex="-1">Incidents</h1>'
    }
    const title = await focusPageTitle(document, { wait })
    expect(title.textContent).toBe('Incidents')
    expect(document.activeElement).toBe(title)
  })

  it('gives up when the page has no title', async () => {
    expect(await focusPageTitle(document, { tries: 3, wait: async () => {} })).toBe(null)
  })
})
