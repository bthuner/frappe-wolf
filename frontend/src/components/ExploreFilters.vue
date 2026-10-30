<script setup>
import { Select, TabButtons, TextInput } from 'frappe-ui'
import { onBeforeUnmount, ref, watch } from 'vue'

import ScopeInputs from '@/components/ScopeInputs.vue'
import { useQueryParam } from '@/composables/useQueryParam'
import { EVENT_TYPES, RANGES, SEVERITIES } from '@/services/filters'

// Window, scope and search shared by the event and error explorers; all of it in the URL.
defineProps({ types: { type: Boolean, default: false } })

const range = useQueryParam('range', '24h', RANGES)
const q = useQueryParam('q')
const type = useQueryParam('type', 'any', EVENT_TYPES)
const severity = useQueryParam('severity', 'any', SEVERITIES)

// typing does not reload on every key: the URL follows after a pause
const draft = ref(q.value)
let timer
watch(draft, (value) => {
  clearTimeout(timer)
  timer = setTimeout(() => (q.value = value.trim()), 400)
})
watch(q, (value) => {
  if (value !== draft.value.trim()) draft.value = value
})
// leaving the page must not let a pending search rewrite the next page's URL
onBeforeUnmount(() => clearTimeout(timer))

const TYPES = ['any', ...EVENT_TYPES].map((v) => ({ value: v, label: v === 'any' ? 'Any type' : v.replace(/_/g, ' ') }))
const SEVERITY_OPTIONS = ['any', ...SEVERITIES].map((v) => ({ value: v, label: v === 'any' ? 'Any severity' : v }))
</script>

<template>
  <div class="flex flex-wrap items-center gap-2">
    <TabButtons v-model="range" :options="RANGES.map((r) => ({ value: r, label: r }))" />
    <TextInput v-model="draft" type="search" placeholder="Title contains" class="w-52" aria-label="Title contains" />
    <ScopeInputs />
    <template v-if="types">
      <Select v-model="type" :options="TYPES" class="w-44" aria-label="Event type" />
      <Select v-model="severity" :options="SEVERITY_OPTIONS" class="w-40" aria-label="Severity" />
    </template>
  </div>
</template>
