<script setup>
import { PageHeader, PageHeaderTitle } from 'frappe-ui'
import { inject } from 'vue'

// Raw-log search needs line storage and a query API that Wolf does not have. Until it does,
// this page explains why and points to the events parsed from the same logs.
const boot = inject('boot')
const logs = boot.capabilities.logs
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Logs" />
  </PageHeader>
  <section class="max-w-2xl space-y-3 p-5 text-base text-ink-gray-8">
    <template v-if="!logs.available">
      <p>Log search is not available. {{ logs.reason }}</p>
      <p>
        The agent reads bench, nginx and auth logs and records what matters in them as events:
        5xx and 502 responses, failed jobs, worker crashes, failed logins.
        <RouterLink class="wolf-link" :to="{ name: 'events' }">Search those events</RouterLink>.
      </p>
    </template>
  </section>
</template>
