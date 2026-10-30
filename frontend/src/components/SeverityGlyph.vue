<script setup>
import { computed } from 'vue'

// brand.md: severity is a moon phase plus its word, never colour alone
const PHASES = {
  info: { glyph: '○', label: 'Info', color: 'var(--wolf-info)' },
  warning: { glyph: '◐', label: 'Warning', color: 'var(--wolf-warning)' },
  critical: { glyph: '●', label: 'Critical', color: 'var(--wolf-critical)' },
  resolved: { glyph: '✓', label: 'Resolved', color: 'var(--wolf-ok)' },
}

const props = defineProps({
  severity: { type: String, default: null },
  showLabel: { type: Boolean, default: true },
})

const phase = computed(() => PHASES[props.severity] || null)
</script>

<template>
  <span v-if="phase" class="inline-flex items-center gap-1" :style="{ color: phase.color }">
    <span aria-hidden="true">{{ phase.glyph }}</span>
    <span :class="showLabel ? '' : 'sr-only'">{{ phase.label }}</span>
  </span>
</template>
