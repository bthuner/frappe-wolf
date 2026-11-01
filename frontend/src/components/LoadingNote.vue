<script setup>
// Says a view is reading, before it has anything to show, so an empty area is never mistaken
// for "nothing there". Announced politely to screen readers.
defineProps({ what: { type: String, default: '' } })
</script>

<template>
  <p role="status" class="text-sm text-ink-gray-6">Loading{{ what ? ` ${what}` : '' }}…</p>
</template>
