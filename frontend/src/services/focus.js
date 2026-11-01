// Keyboard focus that survives what Vue does to the page. Two cases browsers get wrong for us:
//
// - A button that becomes disabled while it works (frappe-ui disables a `loading` Button) drops
//   focus to <body>; the next Tab then starts again from the top of the page.
// - A control that disappears once used (Acknowledge, Approve…, Load more) takes focus with it.
//
// `installFocusGuard` watches for both and puts focus back: on the same control once it is
// enabled again, or, when it is gone, on the control that replaced it (same tag and label in
// the same place), else on the first focusable control left in its nearest surviving container.
// It only ever acts while focus sits on <body> because of such a change: a click on empty space,
// focus moved by the page itself, or a new focus by the user cancels it.
//
// `focusPageTitle` moves focus to the page's <h1> after a route change, as screen readers expect
// from a single-page app: the heading is announced and Tab continues from the new page.

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type=hidden])', 'select:not([disabled])',
  'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]',
].join(',')
const PROGRAMMATIC = '[tabindex="-1"]:not(a[href],button,input,select,textarea)'
const GIVE_UP_MS = 15000 // a control still disabled after this long is not coming back

function label(el) {
  return (el.getAttribute('aria-label') || el.textContent || '').trim()
}

// laid out on the page (`visible` is replaceable: jsdom lays nothing out)
function laidOut(el) {
  return !!(el.offsetParent || el.getClientRects().length)
}

export function isFocusable(el, visible = laidOut) {
  return !!el && el.isConnected && el.matches(FOCUSABLE) && !el.closest('[inert],[aria-hidden="true"]') && visible(el)
}

const FOLLOWING = 4 // Node.DOCUMENT_POSITION_FOLLOWING

// Where each level of the focused control sat when it had focus: its parent and its neighbours.
export function position(el, doc = document) {
  const levels = []
  for (let node = el; node && node.parentElement && node !== doc.body; node = node.parentElement) {
    levels.push({ parent: node.parentElement, prev: node.previousElementSibling, next: node.nextElementSibling })
  }
  return levels
}

// Where focus should go now that `lost` is gone: in its nearest surviving container, at the
// place it left. Its re-rendered copy (same tag and label) first, else the next control after
// that place, else the last one before it.
export function replacement(lost, visible = laidOut) {
  const level = lost.levels.find((l) => l.parent.isConnected)
  if (!level) return null
  const { parent } = level
  const candidates = [...parent.querySelectorAll(FOCUSABLE)].filter((c) => isFocusable(c, visible))
  const prev = level.prev?.isConnected && level.prev.parentElement === parent ? level.prev : null
  const next = level.next?.isConnected && level.next.parentElement === parent ? level.next : null
  const isAfter = (c) =>
    prev ? !!(prev.compareDocumentPosition(c) & FOLLOWING) && !prev.contains(c)
      : next ? c === next || next.contains(c) || !!(next.compareDocumentPosition(c) & FOLLOWING) : true
  const after = candidates.filter(isAfter)
  const before = candidates.filter((c) => !isAfter(c))
  const same = (c) => c.tagName === lost.tag && label(c) === lost.label
  return after.find(same) || before.findLast(same) || after[0] || before.at(-1) || null
}

export function installFocusGuard(doc = document, { giveUpMs = GIVE_UP_MS, visible = laidOut } = {}) {
  let last = null // what had focus: element, its label and where it sat
  let pending = null // a disabled control we wait for
  let timer = null

  function cancel() {
    pending = null
    clearTimeout(timer)
    timer = null
  }

  function onBody() {
    return !doc.activeElement || doc.activeElement === doc.body
  }

  function restore() {
    if (!last) return
    const el = last.el
    if (el.isConnected && el.disabled) {
      // Chromium only drops the focus at its next rendering step: wait whether it has yet or not
      if (pending !== el && (onBody() || doc.activeElement === el)) {
        pending = el
        clearTimeout(timer)
        timer = setTimeout(cancel, giveUpMs)
      }
      return
    }
    if (!onBody()) {
      if (doc.activeElement === el) cancel() // it kept the focus all along
      else if (!el.isConnected) last = null // gone, and focus is elsewhere: nothing to do
      return
    }
    if (el.isConnected) {
      // enabled and still here: put it back only if we were waiting for it
      if (pending === el && isFocusable(el, visible)) el.focus({ preventScroll: true })
      cancel()
      return
    }
    cancel()
    const next = replacement(last, visible)
    last = null // handled once: a control added later never pulls the focus
    if (next) next.focus({ preventScroll: true })
  }

  function onFocusIn(e) {
    const el = e.target
    if (!(el instanceof doc.defaultView.Element) || el === doc.body) return
    cancel()
    // a place focus is only sent to (a page title, a timeline row) is not a control to follow
    last = el.matches(PROGRAMMATIC) ? null : { el, tag: el.tagName, label: label(el), levels: position(el, doc) }
  }

  // a click elsewhere is the user's choice of where focus goes, even if that is nowhere
  function onPointerDown() {
    cancel()
    last = null
  }

  let scheduled = false
  const observer = new doc.defaultView.MutationObserver(() => {
    if (scheduled || !last) return
    scheduled = true
    // after Vue has finished this update, so a re-rendered control can be found
    queueMicrotask(() => {
      scheduled = false
      restore()
    })
  })

  doc.addEventListener('focusin', onFocusIn)
  doc.addEventListener('pointerdown', onPointerDown, true)
  observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['disabled'] })

  return () => {
    cancel()
    observer.disconnect()
    doc.removeEventListener('focusin', onFocusIn)
    doc.removeEventListener('pointerdown', onPointerDown, true)
  }
}

// Focus the new page's <h1 tabindex="-1"> once it has rendered (pages are loaded on demand),
// unless the reader has moved focus meanwhile.
export async function focusPageTitle(doc = document, { tries = 20, wait = (ms) => new Promise((r) => setTimeout(r, ms)) } = {}) {
  const start = doc.activeElement
  for (let i = 0; i < tries; i++) {
    const active = doc.activeElement
    if (active !== start && active !== doc.body && !active?.matches?.('[data-wolf-page-title]')) return null
    const title = doc.querySelector('[data-wolf-page-title]')
    if (title) {
      title.focus({ preventScroll: true })
      return title
    }
    await wait(25)
  }
  return null
}
