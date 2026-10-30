<script setup>
// Exact counts from get_counts: never computed from a list on screen.
defineProps({ counts: { type: Object, required: true } })
</script>

<template>
  <dl class="grid grid-cols-2 gap-3 sm:grid-cols-4">
    <div class="wolf-card">
      <dt class="text-sm text-ink-gray-6">Unresolved incidents</dt>
      <dd class="wolf-data text-2xl text-ink-gray-9">{{ counts.incidents.unresolved }}</dd>
      <dd class="text-sm text-ink-gray-6">{{ counts.incidents.critical_unresolved }} critical</dd>
    </div>
    <div class="wolf-card">
      <dt class="text-sm text-ink-gray-6">Not acknowledged</dt>
      <dd class="wolf-data text-2xl text-ink-gray-9">{{ counts.incidents.unacknowledged }}</dd>
      <dd class="text-sm text-ink-gray-6">open, nobody on it yet</dd>
    </div>
    <div class="wolf-card">
      <dt class="text-sm text-ink-gray-6">Sites with unresolved incidents</dt>
      <dd class="wolf-data text-2xl text-ink-gray-9">{{ counts.incidents.sites_with_unresolved }}</dd>
      <dd class="text-sm text-ink-gray-6">+ {{ counts.incidents.unresolved_without_site }} host or bench-wide</dd>
    </div>
    <div class="wolf-card">
      <dt class="text-sm text-ink-gray-6">Agents reporting</dt>
      <dd class="wolf-data text-2xl text-ink-gray-9">
        {{ counts.agents.agent.fresh }}<span class="text-base text-ink-gray-6"> / {{ counts.agents.agent.fresh + counts.agents.agent.stale + counts.agents.agent.never_seen }}</span>
      </dd>
      <dd class="text-sm text-ink-gray-6">
        {{ counts.agents.agent.stale }} silent<span v-if="counts.agents.satellite.fresh + counts.agents.satellite.stale">,
        satellites {{ counts.agents.satellite.fresh }} reporting / {{ counts.agents.satellite.stale }} silent</span>
      </dd>
    </div>
  </dl>
</template>
