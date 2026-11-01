<script setup>
import { Button, PageHeader } from 'frappe-ui'
import { inject, onMounted, ref } from 'vue'

import LoadingNote from '@/components/LoadingNote.vue'
import PageTitle from '@/components/PageTitle.vue'
import MonitoringPanel from '@/components/dash/MonitoringPanel.vue'
import { call, failure } from '@/services/api'

const boot = inject('boot')
const monitoring = ref(null)
const error = ref(null)
const loading = ref(false)

async function load() {
  loading.value = true
  error.value = null
  try {
    monitoring.value = await call('frappe_wolf.api.dashboard.get_monitoring')
  } catch (e) {
    error.value = failure(e, "Wolf's own monitoring")
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <PageHeader>
    <PageTitle title="Monitoring" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="loading" @click="load" />
  </PageHeader>
  <div class="mx-auto max-w-6xl space-y-5 p-5">
    <p v-if="error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ error }}</p>
    <LoadingNote v-if="loading && !monitoring && !error" />
    <MonitoringPanel v-if="monitoring" :monitoring="monitoring" :time-zone="boot.time_zone" />
  </div>
</template>
