import { ref, shallowRef } from 'vue'

import { call } from '@/services/api'

const METHOD = 'frappe_wolf.api.incident_view.get_timeline'
export const PAGE_SIZE = 100

// Pages of one incident timeline. Only the latest request may write its result: a slow answer
// for an old filter never replaces a newer one. Rows are keyed by their stable id.
export function useTimeline(incident, { fetchFn } = {}) {
  const rows = shallowRef([])
  const paging = ref({ has_more: false, next_cursor: null })
  const meta = ref({ totals: {}, truncated: {}, window: null })
  const loading = ref(false)
  const error = ref(null)
  let ticket = 0

  async function request(args) {
    const mine = ++ticket
    loading.value = true
    error.value = null
    try {
      const page = await call(METHOD, { incident: incident(), ...args }, { fetchFn })
      return mine === ticket ? page : null
    } catch (e) {
      if (mine === ticket) error.value = e.message
      return null
    } finally {
      if (mine === ticket) loading.value = false
    }
  }

  function apply(page, append) {
    const merged = append ? [...rows.value] : []
    const seen = new Set(merged.map((r) => r.id))
    for (const row of page.rows) if (!seen.has(row.id)) merged.push(row) // a repeat never doubles a row
    rows.value = merged
    paging.value = page.paging
    meta.value = { totals: page.totals, truncated: page.truncated, window: page.window }
  }

  // first page, or as many rows as are loaded now (a refresh keeps the reader's place)
  async function load(families, keep = 0) {
    const page = await request({ families: families.join(','), limit: Math.max(PAGE_SIZE, Math.min(keep, 500)) })
    if (page) apply(page, false)
    return page
  }

  async function more(families) {
    if (!paging.value.next_cursor) return null
    const page = await request({ families: families.join(','), cursor: paging.value.next_cursor, limit: PAGE_SIZE })
    if (page) apply(page, true)
    return page
  }

  return { rows, paging, meta, loading, error, load, more }
}
