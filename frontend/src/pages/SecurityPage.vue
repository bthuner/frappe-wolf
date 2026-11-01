<script setup>
import { Button, PageHeader, Select } from 'frappe-ui'
import { computed, inject, ref, watch } from 'vue'

import LoadingNote from '@/components/LoadingNote.vue'
import PageTitle from '@/components/PageTitle.vue'
import { useQueryParam } from '@/composables/useQueryParam'
import { call, failure } from '@/services/api'

// Security posture of one site (frappe_wolf/api/security.py): every check with what Wolf read
// and the fix. A check Wolf cannot read is "Unknown" and does not count as passed.
const STATUS = {
  pass: { label: 'Passed', glyph: '✓', color: 'var(--wolf-ok)' },
  fail: { label: 'Failed', glyph: '✕', color: 'var(--wolf-critical)' },
  unknown: { label: 'Unknown', glyph: '?', color: 'var(--wolf-muted)' },
}
const ORDER = { fail: 0, unknown: 1, pass: 2 }

const site = useQueryParam('site')
const sites = ref([])
const posture = ref(null)
const error = ref(null)
const sitesError = ref(null)
const loading = ref(false)
let ticket = 0

const checks = computed(() => [...(posture.value?.checks || [])].sort((a, b) => ORDER[a.status] - ORDER[b.status] || b.weight - a.weight))
const siteOptions = computed(() => sites.value.map((s) => ({ label: s, value: s })))

async function loadSites() {
  try {
    sites.value = await call('frappe_wolf.api.security.list_sites')
  } catch (e) {
    sitesError.value = failure(e, 'the sites')
  }
}

async function load() {
  const mine = ++ticket
  loading.value = true
  error.value = null
  try {
    const result = await call('frappe_wolf.api.security.get_posture', { site: site.value || undefined })
    if (mine === ticket) posture.value = result
  } catch (e) {
    if (mine === ticket) {
      posture.value = null
      error.value = failure(e, 'this site')
    }
  } finally {
    if (mine === ticket) loading.value = false
  }
}

// tamper-evident journal (frappe_wolf/api/journal.py), managers only
const boot = inject('boot', {})
const journal = ref(null)
const verdict = ref(null)
const verifying = ref(false)
async function loadJournal() {
  if (!boot?.can?.configure) return
  try {
    journal.value = await call('frappe_wolf.api.journal.get_status')
  } catch {
    journal.value = null
  }
}
async function verifyJournal() {
  verifying.value = true
  try {
    verdict.value = await call('frappe_wolf.api.journal.verify', {}, { post: true })
  } catch (e) {
    verdict.value = { ok: false, problem: { why: e.message } }
  } finally {
    verifying.value = false
  }
}

loadSites()
loadJournal()
watch(site, load, { immediate: true })
</script>

<template>
  <PageHeader>
    <PageTitle title="Security" />
    <div class="flex items-center gap-2">
      <Select
        v-if="siteOptions.length > 1"
        :model-value="posture?.site || site"
        :options="siteOptions"
        aria-label="Site"
        class="w-64"
        @update:model-value="(v) => (site = v)"
      />
      <Button label="Refresh" icon-left="lucide-refresh-cw" :loading="loading" @click="load" />
    </div>
  </PageHeader>

  <div class="mx-auto max-w-5xl space-y-5 p-5">
    <p v-if="sitesError" role="alert" class="text-sm" style="color: var(--wolf-critical)">Sites: {{ sitesError }}</p>
    <p v-if="error" role="alert" class="text-sm" style="color: var(--wolf-critical)">{{ error }}</p>

    <LoadingNote v-if="loading && !posture && !error" what="the security score" />
    <template v-if="posture">
      <section class="wolf-card flex flex-wrap items-baseline gap-x-8 gap-y-2" aria-label="Score">
        <div>
          <div class="wolf-data text-3xl font-semibold text-ink-gray-9">{{ posture.score }}<span class="text-base text-ink-gray-6"> / 100</span></div>
          <div class="text-sm text-ink-gray-6">security score of {{ posture.site }}</div>
        </div>
        <dl class="wolf-facts text-sm">
          <dt>Checks</dt>
          <dd>{{ posture.passed }} passed, {{ posture.failed }} failed, {{ posture.unknown }} unknown</dd>
          <dt>Coverage</dt>
          <dd>Wolf could read {{ posture.coverage }}% of the weight</dd>
          <dt v-if="posture.hosts.length">Hosts</dt>
          <dd v-if="posture.hosts.length">{{ posture.hosts.join(', ') }}</dd>
        </dl>
        <p class="w-full text-sm text-ink-gray-6">
          The score is the weight of the checks that passed out of all of them: a check Wolf cannot read counts as not
          passed. Agent samples older than {{ posture.recent_hours }} h are not used.
          <template v-if="!posture.local">System Settings checks are only readable on the site itself.</template>
        </p>
      </section>

      <section aria-label="Checks">
        <table class="wolf-card w-full text-left text-sm">
          <thead class="text-ink-gray-6">
            <tr>
              <th class="py-1 font-normal">Check</th>
              <th class="py-1 font-normal">Result</th>
              <th class="py-1 font-normal">What Wolf read</th>
              <th class="py-1 text-right font-normal">Weight</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in checks" :key="c.id" class="border-t border-outline-gray-1 align-top">
              <td class="py-2 pr-3 text-ink-gray-9">{{ c.label }}</td>
              <td class="py-2 pr-3 whitespace-nowrap">
                <span :style="{ color: STATUS[c.status].color }" aria-hidden="true">{{ STATUS[c.status].glyph }}</span>
                {{ STATUS[c.status].label }}
              </td>
              <td class="py-2 pr-3 text-ink-gray-7">
                {{ c.evidence }}
                <p v-if="c.fix" class="mt-1 text-ink-gray-9">Fix: {{ c.fix }}</p>
              </td>
              <td class="wolf-data py-2 text-right">{{ c.weight }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <section v-if="journal" class="wolf-card space-y-2" aria-label="Journal">
      <div class="flex flex-wrap items-baseline justify-between gap-2">
        <h2 class="text-base font-semibold text-ink-gray-9">Tamper-evident journal</h2>
        <Button size="sm" label="Verify" :loading="verifying" @click="verifyJournal" />
      </div>
      <p class="text-sm text-ink-gray-7">
        {{ journal.entries }} signed entries, {{ journal.pending }} waiting to be signed (within a minute).
        <template v-if="!journal.key">No key yet: it is created with the first signature.</template>
      </p>
      <p v-if="journal.anchor" class="text-sm text-ink-gray-7">
        Anchor, to copy somewhere Wolf cannot write (a ticket, a password manager):
        <code class="wolf-data break-all">{{ journal.anchor }}</code>
      </p>
      <p v-if="verdict" role="status" class="text-sm" :style="{ color: verdict.ok ? 'var(--wolf-ok)' : 'var(--wolf-critical)' }">
        <template v-if="verdict.ok">Verified: {{ verdict.checked }} entries, every digest and MAC matches, no gap.</template>
        <template v-else>Broken<template v-if="verdict.problem?.seq"> at entry {{ verdict.problem.seq }}</template>: {{ verdict.problem?.why }}</template>
      </p>
    </section>
  </div>
</template>
