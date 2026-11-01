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

// Where focus should go now that `lost` is gone, from what was recorded when it had focus.
export function replacement(lost, visible = laidOut) {
  const ancestor = lost.ancestors.find((a) => a.isConnected)
  if (!ancestor) return null
  const candidates = [...ancestor.querySelectorAll(FOCUSABLE)].filter((c) => isFocusable(c, visible))
  const same = candidates.find((c) => c.tagName === lost.tag && label(c) === lost.label)
  return same || candidates[0] || null
}

export function installFocusGuard(doc = document, { giveUpMs = GIVE_UP_MS, visible = laidOut } = {}) {
  let last = null // what had focus: element, its label and its containers
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
    if (next) next.focus({ preventScroll: true })
  }

  function onFocusIn(e) {
    const el = e.target
    if (!(el instanceof doc.defaultView.Element) || el === doc.body) return
    cancel()
    const ancestors = []
    for (let a = el.parentElement; a && a !== doc.body; a = a.parentElement) ancestors.push(a)
    last = { el, tag: el.tagName, label: label(el), ancestors }
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

// Focus the new page's <h1 tabindex="-1"> once it has rendered (pages are loaded on demand).
export async function focusPageTitle(doc = document, { tries = 20, wait = (ms) => new Promise((r) => setTimeout(r, ms)) } = {}) {
  for (let i = 0; i < tries; i++) {
    const title = doc.querySelector('[data-wolf-page-title]')
    if (title) {
      title.focus({ preventScroll: true })
      return title
    }
    await wait(25)
  }
  return null
}
