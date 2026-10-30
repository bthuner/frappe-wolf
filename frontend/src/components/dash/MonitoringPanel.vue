<script setup>
import { computed } from 'vue'

import { ago, full } from '@/services/time'

// Is Wolf itself working. Each part is shown on its own: an online agent says nothing about
// the rules running, and neither proves the agent's watchdog can alert.
const props = defineProps({
  monitoring: { type: Object, required: true },
  timeZone: { type: String, required: true },
  compact: { type: Boolean, default: false },
})

const health = computed(() => props.monitoring.health)
const agents = computed(() => props.monitoring.agents.filter((a) => a.kind === 'agent'))
const satellites = computed(() => props.monitoring.agents.filter((a) => a.kind === 'satellite'))
const STATE = { fresh: 'reporting', stale: 'silent', never_seen: 'never reported', disabled: 'disabled' }
</script>

<template>
  <section class="wolf-card space-y-3" aria-label="Monitoring">
    <h2 class="text-base font-semibold text-ink-gray-9">Is Wolf working</h2>
    <dl class="wolf-facts text-sm">
      <dt>Alert rules</dt>
      <dd v-if="!health">Unknown: the site could not compute its health.</dd>
      <dd v-else-if="health.rules.last_evaluated">
        evaluated <time :title="full(health.rules.last_evaluated, timeZone)">{{ ago(health.rules.last_evaluated) }} ago</time>
        <span v-if="health.problems.length" style="color: var(--wolf-critical)"> · {{ health.problems.join('; ') }}</span>
      </dd>
      <dd v-else>no rule has run yet</dd>
      <template v-if="health">
        <dt>Scheduler</dt>
        <dd>{{ health.scheduler.enabled ? 'enabled' : `disabled (${health.scheduler.reason})` }}</dd>
        <dt>Ingest queue</dt>
        <dd>
          <template v-if="health.queues.error">unreachable: {{ health.queues.error }}</template>
          <template v-else-if="health.queues.ingest_backlog_seconds != null">oldest push waiting {{ Math.round(health.queues.ingest_backlog_seconds / 60) }} min</template>
          <template v-else>empty</template>
          <template v-if="health.queues.workers != null"> · {{ health.queues.workers }} workers</template>
        </dd>
      </template>
      <dt>Rollups done until</dt>
      <dd class="wolf-data">1m {{ monitoring.rollups['1m'] ? full(monitoring.rollups['1m'], timeZone) : 'not yet' }}</dd>
      <dt>Agents</dt>
      <dd>
        {{ agents.filter((a) => a.state === 'fresh').length }} of {{ agents.length }} reporting
        (silent after {{ monitoring.silence_minutes }} min)
      </dd>
      <template v-if="satellites.length">
        <dt>Satellites</dt>
        <dd>{{ satellites.filter((a) => a.state === 'fresh').length }} of {{ satellites.length }} forwarding</dd>
      </template>
      <template v-if="monitoring.mode.mode === 'satellite'">
        <dt>This site</dt>
        <dd>satellite of {{ monitoring.mode.central_url }}; {{ monitoring.mode.pending }} items waiting to forward</dd>
      </template>
    </dl>
    <table v-if="!compact && monitoring.agents.length" class="w-full text-left text-sm">
      <thead class="text-ink-gray-6">
        <tr><th class="py-1 font-normal">Host or site</th><th class="font-normal">Kind</th><th class="font-normal">State</th><th class="font-normal">Last seen</th><th class="font-normal">Clock skew</th><th class="font-normal">Version</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in monitoring.agents" :key="a.name" class="border-t border-[var(--wolf-line)]">
          <td class="py-1">{{ a.hostname }}</td>
          <td>{{ a.kind }}</td>
          <td>{{ STATE[a.state] }}</td>
          <td class="wolf-data">{{ a.last_seen ? `${ago(a.last_seen)} ago` : '' }}</td>
          <td class="wolf-data">{{ a.clock_skew_seconds != null ? `${a.clock_skew_seconds} s` : 'unknown' }}</td>
          <td class="wolf-data">{{ a.agent_version || '' }}</td>
        </tr>
      </tbody>
    </table>
    <p class="text-sm text-ink-gray-6">{{ monitoring.note }}</p>
  </section>
</template>
