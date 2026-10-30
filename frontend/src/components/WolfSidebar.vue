<script setup>
import { Sidebar, SidebarHeader, SidebarItem, SidebarLabel, useColorScheme } from 'frappe-ui'

import { logout } from '@/services/boot'
import { computed, inject } from 'vue'

const boot = inject('boot')
const { colorScheme, setColorScheme } = useColorScheme()

const SCHEMES = [
  { value: 'light', label: 'Light theme' },
  { value: 'dark', label: 'Dark theme' },
  { value: 'system', label: 'System theme' },
]

const menu = computed(() => [
  ...SCHEMES.map((s) => ({
    label: s.label,
    selected: colorScheme.value === s.value,
    onClick: () => setColorScheme(s.value),
  })),
  { label: 'Open Desk', icon: 'lucide-external-link', onClick: () => window.location.assign(boot.desk.home) },
  { label: 'Log out', icon: 'lucide-log-out', onClick: () => logout() },
])
</script>

<template>
  <Sidebar aria-label="Wolf">
    <SidebarHeader
      title="Frappe Wolf"
      :subtitle="boot.user.full_name"
      logo="/assets/frappe_wolf/images/wolf.png"
      :menu-items="menu"
    />
    <div class="flex-1 overflow-y-auto px-2 pb-2">
      <SidebarItem label="Overview" icon="lucide-layout-dashboard" :route="{ name: 'overview' }" />
      <SidebarItem
        v-if="boot.capabilities.incidents.available"
        label="Incidents"
        icon="lucide-siren"
        :route="{ name: 'incidents' }"
      />
      <SidebarItem label="Events" icon="lucide-list" :route="{ name: 'events' }" />
      <SidebarItem label="Errors" icon="lucide-bug" :route="{ name: 'errors' }" />
      <SidebarLabel>Explore</SidebarLabel>
      <SidebarItem label="Metrics" icon="lucide-chart-line" :route="{ name: 'metrics' }" />
      <SidebarItem label="Logs" icon="lucide-scroll-text" :route="{ name: 'logs' }" />
      <SidebarLabel>Wolf</SidebarLabel>
      <SidebarItem label="Monitoring" icon="lucide-activity" :route="{ name: 'monitoring' }" />
      <SidebarItem
        v-if="boot.can.configure"
        label="Alert rules (Desk)"
        icon="lucide-bell"
        :href="boot.desk.fallbacks.alert_rules"
      />
    </div>
  </Sidebar>
</template>
