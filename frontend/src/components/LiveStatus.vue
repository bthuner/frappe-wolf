<script setup>
import { Button } from 'frappe-ui'
import { computed } from 'vue'

// What keeps the view current, said plainly: live, reconnecting, polling, or paused.
const props = defineProps({
  live: { type: Object, required: true },
  pollSeconds: { type: Number, default: 60 },
  pendingLabel: { type: String, default: 'Changes waiting' },
})

const label = computed(() => {
  const l = props.live
  if (l.paused.value) return 'Paused'
  if (l.state.value === 'live') return 'Live'
  if (l.state.value === 'connecting') return 'Connecting…'
  if (l.state.value === 'polling') return l.polls ? `Checks every ${props.pollSeconds} s` : 'Not live: refresh by hand'
  return 'Refresh by hand'
})
// a known change, or one that may have been missed while the socket was away
const pendingText = computed(() =>
  props.live.pendingReason?.value === 'reconnect' ? 'Reconnected, changes may have been missed' : props.pendingLabel,
)
</script>

<template>
  <div class="flex items-center gap-2 text-sm text-ink-gray-6">
    <span role="status" aria-live="polite" class="wolf-live" :data-state="live.paused.value ? 'paused' : live.state.value">{{ label }}</span>
    <Button
      v-if="live.state.value !== 'off'"
      size="sm"
      variant="ghost"
      :label="live.paused.value ? 'Resume' : 'Pause'"
      :aria-pressed="live.paused.value"
      @click="live.toggle()"
    />
    <Button v-if="live.pending.value" size="sm" variant="subtle" :label="`${pendingText} · Refresh`" @click="live.apply()" />
  </div>
</template>
