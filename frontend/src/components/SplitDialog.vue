<script setup>
import { Button, Dialog, TextInput } from 'frappe-ui'
import { computed, ref, watch } from 'vue'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import { call } from '@/services/api'
import { clock, full } from '@/services/time'

// Split some linked events into a new incident (api/incident.py split). The server says what the
// new incident would be before it exists; at least one event stays. A split incident has no rule,
// so nothing resolves it by itself.
const props = defineProps({
  incident: { type: String, required: true },
  timeZone: { type: String, required: true },
})
const open = defineModel('open', { type: Boolean, default: false })
const emit = defineEmits(['split'])

const data = ref(null)
const selected = ref([])
const title = ref('')
const loading = ref(false)
const splitting = ref(false)
const error = ref(null)
let request = 0

async function load() {
  const mine = ++request
  loading.value = true
  error.value = null
  try {
    const args = { incident: props.incident }
    if (selected.value.length) args.events = JSON.stringify(selected.value)
    const answer = await call('frappe_wolf.api.incident_view.split_preview', args)
    if (mine === request) data.value = answer
  } catch (e) {
    if (mine === request) error.value = e.message
  } finally {
    if (mine === request) loading.value = false
  }
}

watch(open, (value) => {
  if (!value) return
  selected.value = []
  title.value = ''
  data.value = null
  load()
})
watch(selected, () => open.value && load(), { deep: true })

const preview = computed(() => (selected.value.length ? data.value?.preview : null))
const plural = (n, noun) => `${n} ${n === 1 ? noun : `${noun}s`}`

async function split() {
  splitting.value = true
  error.value = null
  try {
    const name = await call(
      'frappe_wolf.api.incident.split',
      { incident: props.incident, events: JSON.stringify(selected.value), title: title.value.trim() || null },
      { post: true },
    )
    open.value = false
    emit('split', name)
  } catch (e) {
    error.value = e.message // the server's reason; the selection and title stay
  } finally {
    splitting.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="open" :title="`Split events out of ${incident}`" size="2xl">
    <div class="space-y-3 text-base text-ink-gray-8">
      <p class="text-sm text-ink-gray-7">
        The events you tick move to a new incident with the same scope; the others stay. The new incident has no alert rule:
        it never resolves by itself.
      </p>
      <fieldset class="max-h-80 overflow-auto rounded border border-outline-gray-1">
        <legend class="sr-only">Events linked to {{ incident }}</legend>
        <p v-if="!data" class="p-3 text-sm text-ink-gray-6">Loading…</p>
        <label
          v-for="e in data?.events || []"
          :key="e.name"
          class="flex cursor-pointer items-baseline gap-2 border-b border-outline-gray-1 px-3 py-2 text-sm last:border-b-0"
        >
          <input v-model="selected" type="checkbox" :value="e.name" class="self-center" />
          <time class="wolf-data shrink-0" :datetime="e.at" :title="full(e.at, timeZone)">{{ clock(e.at, timeZone) }}</time>
          <SeverityGlyph :severity="e.severity" :show-label="false" />
          <span class="min-w-0 flex-1 text-ink-gray-9">{{ e.title }}</span>
          <span v-if="e.count > 1" class="wolf-data text-ink-gray-6">{{ e.count }} reported</span>
        </label>
      </fieldset>
      <p v-if="data?.more_events" class="text-sm text-ink-gray-6">The first {{ data.events.length }} linked events are listed.</p>
      <TextInput v-model="title" placeholder="Title of the new incident (optional)" aria-label="Title of the new incident" />

      <div v-if="preview" class="wolf-card space-y-2" aria-live="polite" data-testid="split-preview">
        <p v-if="preview.error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ preview.error }}</p>
        <template v-else>
          <p><b>Moves</b> {{ plural(preview.moved, 'event') }} to a new incident; {{ plural(preview.kept, 'event') }} {{ preview.kept === 1 ? 'stays' : 'stay' }} in {{ incident }}.</p>
          <p class="flex flex-wrap items-baseline gap-x-2">
            <b>New incident</b> {{ preview.new_incident.status }},
            <SeverityGlyph :severity="preview.new_incident.severity" />
            <span>since <time :datetime="preview.new_incident.started_at">{{ full(preview.new_incident.started_at, timeZone) }}</time>:</span>
            <span>{{ title.trim() || preview.new_incident.title }}</span>
          </p>
        </template>
      </div>
      <p v-if="error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ error }}</p>
    </div>
    <template #actions>
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="open = false" />
        <Button
          variant="solid"
          :label="selected.length ? `Split ${plural(selected.length, 'event')}` : 'Split'"
          :disabled="!preview || !!preview.error || loading"
          :loading="splitting"
          @click="split"
        />
      </div>
    </template>
  </Dialog>
</template>
