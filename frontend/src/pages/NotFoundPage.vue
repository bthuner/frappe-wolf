<script setup>
import { PageHeader, PageHeaderTitle } from 'frappe-ui'
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Not found" />
  </PageHeader>
  <section class="max-w-2xl space-y-3 p-5 text-base text-ink-gray-8">
    <p>
      There is no page at <span class="wolf-data">/wolf{{ route.path }}</span>.
      <RouterLink class="wolf-link" :to="{ name: 'overview' }">Go to the overview</RouterLink>.
    </p>
  </section>
</template>
