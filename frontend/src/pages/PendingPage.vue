<script setup>
import { PageHeader, PageHeaderTitle } from 'frappe-ui'
import { computed, inject } from 'vue'
import { useRoute } from 'vue-router'

import { deskFallback } from '@/services/boot'

// A view of #19 that is not built yet. It says so and hands over to the Desk page that does
// the job today; it never shows made-up numbers.
const route = useRoute()
const boot = inject('boot')
const desk = computed(() => deskFallback(boot, route))
// a satellite keeps no incidents: its central site opens them
const unavailable = computed(() => route.meta.capability && !boot.capabilities?.[route.meta.capability]?.available)
const subject = computed(() => Object.values(route.params).filter(Boolean).join(' / '))
</script>

<template>
  <PageHeader>
    <PageHeaderTitle :title="subject ? `${route.meta.title}: ${subject}` : route.meta.title" />
  </PageHeader>
  <section v-if="unavailable" class="max-w-2xl space-y-3 p-5 text-base text-ink-gray-8">
    <p>
      This site is a satellite: it forwards its telemetry to a central Wolf site, which opens and
      keeps the incidents. Look for {{ route.meta.title.toLowerCase() }} there.
    </p>
  </section>
  <section v-else class="max-w-2xl space-y-3 p-5 text-base text-ink-gray-8">
    <p>
      This view is not built yet. When it is, it will show:
      <span class="text-ink-gray-9">{{ route.meta.planned }}</span>
    </p>
    <p v-if="desk">
      Desk has it today:
      <a class="wolf-link" :href="desk">open {{ route.meta.title.toLowerCase() }} in Desk</a>.
    </p>
  </section>
</template>
