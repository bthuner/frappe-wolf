<script setup>
import { Button, PageHeader, PageHeaderTitle, TabButtons, Textarea, TextInput } from 'frappe-ui'
import { computed, inject, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import SeverityGlyph from '@/components/SeverityGlyph.vue'
import TimelineRow from '@/components/TimelineRow.vue'
import { useTimeline } from '@/composables/useTimeline'
import { call } from '@/services/api'
import { clock, day, duration, full } from '@/services/time'

const route = useRoute()
const router = useRouter()
const boot = inject('boot')
const tz = boot.time_zone

const SHOW = [
  { value: 'all', label: 'All', families: ['evidence', 'context', 'activity', 'delivery'] },
  { value: 'evidence', label: 'Evidence', families: ['evidence'] },
  { value: 'context', label: 'Context', families: ['context'] },
  { value: 'activity', label: 'Activity', families: ['activity'] },
  { value: 'delivery', label: 'Notifications', families: ['delivery'] },
]
const GAP_MINUTES = 60

const name = computed(() => route.params.incident)
const show = computed({
  get: () => (SHOW.some((s) => s.value === route.query.show) ? route.query.show : 'all'),
  set: (value) => router.replace({ query: { ...route.query, show: value === 'all' ? undefined : value }, hash: route.hash }),
})
const query = computed({
  get: () => route.query.q || '',
  set: (value) => router.replace({ query: { ...route.query, q: value || undefined }, hash: route.hash }),
})
const families = computed(() => SHOW.find((s) => s.value === show.value).families)

const incident = ref(null)
const loadError = ref(null)
const actionError = ref(null)
const acting = ref(null)
const comment = ref('')
const posting = ref(false)
const expanded = ref(new Set())
const timeline = useTimeline(() => name.value)

function anchor(row) {
  return `row-${row.id.replace(/[^A-Za-z0-9_-]/g, '-')}`
}

// a search filters the rows already loaded, and says so
const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return timeline.rows.value
  return timeline.rows.value.filter((r) =>
    [r.summary, r.text, r.category, r.source, r.channel, r.target, r.user_name].some((v) => v && String(v).toLowerCase().includes(q)),
  )
})

// date separators and long silences between rows, never a "healthy" claim
const items = computed(() => {
  const out = []
  let lastDay = null
  let lastAt = null
  for (const row of visible.value) {
    const rowDay = day(row.at, tz)
    if (rowDay !== lastDay) out.push({ type: 'day', key: `day-${rowDay}`, label: rowDay })
    else if (lastAt && (new Date(row.at) - new Date(lastAt)) / 60000 >= GAP_MINUTES) {
      out.push({ type: 'gap', key: `gap-${row.id}`, label: duration(lastAt, row.at) })
    }
    out.push({ type: 'row', key: row.id, row })
    lastDay = rowDay
    lastAt = row.at
  }
  return out
})
const filtered = computed(() => show.value !== 'all' || query.value.trim() !== '')

const scope = computed(() => {
  const s = incident.value?.scope || {}
  return [s.site, s.bench, s.host].filter(Boolean).join(' · ') // site, then bench, then host
})
const openFor = computed(() => {
  const i = incident.value
  if (!i) return ''
  return i.resolved.at ? `lasted ${duration(i.started_at, i.resolved.at)}` : `open for ${duration(i.started_at, new Date().toISOString())}`
})

async function loadIncident() {
  loadError.value = null
  try {
    incident.value = await call('frappe_wolf.api.incident_view.get_incident', { incident: name.value })
  } catch (e) {
    incident.value = null
    loadError.value = e.status === 403 ? 'You may not see this incident.' : e.message
  }
}

async function refresh() {
  await loadIncident()
  if (incident.value) await timeline.load(families.value, timeline.rows.value.length)
}

async function act(action) {
  acting.value = action
  actionError.value = null
  try {
    incident.value = await call('frappe_wolf.api.incident_view.act', { incident: name.value, action }, { post: true })
    await timeline.load(families.value, timeline.rows.value.length)
  } catch (e) {
    actionError.value = e.message // the server's reason; nothing changed on screen
  } finally {
    acting.value = null
  }
}

async function postComment() {
  posting.value = true
  actionError.value = null
  try {
    await call('frappe_wolf.api.incident.add_comment', { incident: name.value, content: comment.value }, { post: true })
    comment.value = '' // cleared only once it is saved
    await timeline.load(families.value, timeline.rows.value.length + 1)
  } catch (e) {
    actionError.value = e.message
  } finally {
    posting.value = false
  }
}

function toggle(id) {
  const next = new Set(expanded.value)
  next.has(id) ? next.delete(id) : next.add(id)
  expanded.value = next
}

async function revealHash() {
  if (!route.hash) return
  const row = timeline.rows.value.find((r) => `#${anchor(r)}` === route.hash)
  if (!row) return
  expanded.value = new Set([...expanded.value, row.id])
  await nextTick()
  document.getElementById(anchor(row))?.scrollIntoView({ block: 'center' })
}

onMounted(async () => {
  await refresh()
  await revealHash()
})
watch(families, () => timeline.load(families.value))
watch(name, refresh)
</script>

<template>
  <PageHeader>
    <PageHeaderTitle :title="`Incident ${name}`" />
    <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="timeline.loading.value" @click="refresh" />
  </PageHeader>

  <section v-if="loadError" class="max-w-2xl p-5 text-base text-ink-gray-8">
    <p>{{ loadError }}</p>
    <RouterLink class="wolf-link" :to="{ name: 'incidents' }">Back to incidents</RouterLink>
  </section>

  <div v-else-if="incident" class="mx-auto max-w-5xl space-y-5 p-5">
    <header class="wolf-card space-y-2">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-ink-gray-6">
        <SeverityGlyph :severity="incident.severity" />
        <span class="wolf-badge">{{ incident.status }}</span>
        <span v-if="scope">{{ scope }}</span>
      </div>
      <h1 class="text-xl font-semibold text-ink-gray-9">{{ incident.title }}</h1>
      <dl class="wolf-facts text-sm">
        <dt>Started</dt>
        <dd><time class="wolf-data" :datetime="incident.started_at">{{ full(incident.started_at, tz) }}</time>, {{ openFor }}</dd>
        <template v-if="incident.acknowledged.at">
          <dt>Acknowledged</dt>
          <dd>{{ incident.acknowledged.by_name || incident.acknowledged.by }}, <time class="wolf-data">{{ clock(incident.acknowledged.at, tz) }}</time></dd>
        </template>
        <template v-if="incident.resolved.at">
          <dt>Resolved</dt>
          <dd>{{ incident.resolved.by_name || incident.resolved.by || 'automatically' }}, <time class="wolf-data">{{ clock(incident.resolved.at, tz) }}</time></dd>
        </template>
        <dt>Owner</dt>
        <dd>{{ incident.assigned_to ? incident.assigned_to.name : 'Nobody yet' }}</dd>
        <template v-if="incident.deployment">
          <dt>Deployment before it</dt>
          <dd>
            <time class="wolf-data">{{ full(incident.deployment.at, tz) }}</time>
            <span v-if="!incident.deployment.available">, details deleted by retention</span>
          </dd>
        </template>
        <template v-if="incident.alert_rule">
          <dt>Rule</dt>
          <dd>{{ incident.alert_rule.name }}</dd>
        </template>
        <template v-if="incident.silenced">
          <dt>Silenced</dt>
          <dd>Yes: no notifications are sent for it</dd>
        </template>
        <dt>Linked events</dt>
        <dd>{{ incident.event_records }} records, {{ incident.event_count }} reported occurrences</dd>
      </dl>
      <p v-if="incident.merged_into" class="text-sm">
        Merged into
        <RouterLink class="wolf-link" :to="{ name: 'incident', params: { incident: incident.merged_into } }">{{ incident.merged_into }}</RouterLink>.
      </p>
      <div class="flex flex-wrap items-center gap-2 pt-1">
        <Button v-if="incident.can.acknowledge" variant="solid" label="Acknowledge" :loading="acting === 'acknowledge'" @click="act('acknowledge')" />
        <Button v-if="incident.can.assign_to_me" label="Assign to me" :loading="acting === 'assign_to_me'" @click="act('assign_to_me')" />
        <Button v-if="incident.can.resolve" label="Resolve" :loading="acting === 'resolve'" @click="act('resolve')" />
        <a v-if="incident.can.workflow" class="wolf-link text-sm" :href="`/app/wolf-incident/${encodeURIComponent(incident.name)}`">
          Merge, split or write the postmortem in Desk
        </a>
      </div>
      <p v-if="actionError" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ actionError }}</p>
    </header>

    <section class="space-y-3" aria-label="Timeline">
      <div class="flex flex-wrap items-center gap-3">
        <TabButtons v-model="show" :options="SHOW.map(({ value, label }) => ({ value, label }))" />
        <TextInput v-model="query" type="search" placeholder="Search loaded rows" class="w-56" aria-label="Search loaded rows" />
        <span class="text-sm text-ink-gray-6">Times in {{ tz }}</span>
      </div>
      <p v-if="query.trim()" class="text-sm text-ink-gray-6">
        {{ visible.length }} of {{ timeline.rows.value.length }} loaded rows match<span v-if="timeline.paging.value.has_more">; more rows are not loaded yet</span>.
      </p>
      <p v-for="(cut, key) in timeline.meta.value.truncated" :key="key" class="text-sm text-ink-gray-6">
        Activity shows the newest {{ cut.shown }} of {{ cut.total }} {{ key === 'activity_version' ? 'recorded changes' : 'comments' }};
        older ones are in Desk.
      </p>
      <p v-if="timeline.error.value" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ timeline.error.value }}</p>

      <ol class="wolf-rail">
        <template v-for="item in items" :key="item.key">
          <li v-if="item.type === 'day'" class="wolf-day">{{ item.label }}</li>
          <li v-else-if="item.type === 'gap'" class="wolf-gap">
            {{ item.label }} with no {{ filtered ? 'matching rows' : 'recorded activity' }}
          </li>
          <TimelineRow
            v-else
            :row="item.row"
            :time-zone="tz"
            :anchor="anchor(item.row)"
            :targeted="route.hash === `#${anchor(item.row)}`"
            :expanded="expanded.has(item.row.id)"
            @toggle="toggle(item.row.id)"
          />
        </template>
      </ol>
      <p v-if="!timeline.loading.value && !visible.length" class="text-sm text-ink-gray-6">
        {{ filtered ? 'No rows match this filter.' : 'Nothing recorded in this window.' }}
      </p>
      <Button v-if="timeline.paging.value.has_more" label="Load more" :loading="timeline.loading.value" @click="timeline.more(families)" />
    </section>

    <section class="wolf-card space-y-2" aria-label="Comment">
      <Textarea v-model="comment" :rows="3" placeholder="Add a comment for the team" aria-label="Comment" />
      <Button label="Comment" :disabled="!comment.trim()" :loading="posting" @click="postComment" />
    </section>
  </div>
</template>
