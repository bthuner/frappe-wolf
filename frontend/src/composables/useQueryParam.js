import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// A filter kept in the URL (shareable, survives reload and back). `fallback` is not written.
export function useQueryParam(name, fallback = '', allowed = null) {
  const route = useRoute()
  const router = useRouter()
  return computed({
    get: () => {
      const v = route.query[name]
      if (v == null || v === '') return fallback
      return allowed && !allowed.includes(v) ? fallback : v
    },
    set: (value) => {
      const query = { ...route.query, [name]: value === fallback || value === '' ? undefined : value }
      delete query.cursor
      router.replace({ query, hash: route.hash })
    },
  })
}
