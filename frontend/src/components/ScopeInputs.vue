<script setup>
import { TextInput } from 'frappe-ui'

import { useQueryParam } from '@/composables/useQueryParam'

// Site, host and bench in the URL. A bench is named on its host: its field appears once a host is set.
const site = useQueryParam('site')
const host = useQueryParam('host')
const bench = useQueryParam('bench')
</script>

<template>
  <TextInput :model-value="site" placeholder="Site" class="w-40" aria-label="Site" @change="site = $event.target.value.trim()" />
  <TextInput :model-value="host" placeholder="Host" class="w-36" aria-label="Host" @change="host = $event.target.value.trim()" />
  <TextInput v-if="host" :model-value="bench" placeholder="Bench" class="w-36" aria-label="Bench" @change="bench = $event.target.value.trim()" />
</template>
