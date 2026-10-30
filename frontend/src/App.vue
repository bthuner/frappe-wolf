<script setup>
import { DesktopShell, FrappeUIProvider } from 'frappe-ui'

import WolfSidebar from '@/components/WolfSidebar.vue'
</script>

<template>
  <FrappeUIProvider>
    <div class="h-screen">
      <DesktopShell>
        <template #sidebar><WolfSidebar /></template>
        <RouterView />
      </DesktopShell>
    </div>
  </FrappeUIProvider>
</template>
