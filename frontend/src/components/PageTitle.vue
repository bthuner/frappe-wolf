<script setup>
// The page's <h1>, in the header frappe-ui's PageHeader pins above the scroll. tabindex="-1":
// not a Tab stop, but focus moves here after each route change (services/focus.js).
defineProps({ title: { type: String, required: true } })
</script>

<template>
  <h1 data-wolf-page-title tabindex="-1" class="wolf-page-title truncate text-lg font-semibold text-ink-gray-9">{{ title }}</h1>
</template>
