import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { call } from '@/services/api'

// A paged list driven by the URL's filters: a newer filter's answer always wins, and
// "Load more" appends without repeating a row.
export function useExplore(method, key, { extraArgs = () => ({}), itemKey }) {
  const route = useRoute()
  const items = ref([])
  const meta = ref(null)
  const cursor = ref(null)
  const hasMore = ref(false)
  const loading = ref(false)
  const error = ref(null)
  let ticket = 0

  const args = computed(() => {
    const q = route.query
    const out = { range: q.range || '24h', q: q.q, site: q.site, host: q.host, bench: q.host ? q.bench : undefined, ...extraArgs(q) }
    return Object.fromEntries(Object.entries(out).filter(([, v]) => v != null && v !== ''))
  })

  async function load(append = false) {
    const mine = ++ticket
    loading.value = true
    error.value = null
    try {
      const page = await call(method, { ...args.value, cursor: append ? cursor.value : undefined })
      if (mine !== ticket) return
      const seen = new Set(append ? items.value.map(itemKey) : [])
      items.value = [...(append ? items.value : []), ...page[key].filter((i) => !seen.has(itemKey(i)))]
      cursor.value = page.paging.next_cursor
      hasMore.value = page.paging.has_more
      meta.value = page
    } catch (e) {
      if (mine === ticket) error.value = e.message
    } finally {
      if (mine === ticket) loading.value = false
    }
  }

  watch(args, () => load(), { immediate: true })
  return { items, meta, hasMore, loading, error, load }
}
