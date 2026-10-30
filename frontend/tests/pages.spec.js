import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import LogsPage from '@/pages/LogsPage.vue'
import PendingPage from '@/pages/PendingPage.vue'
import { makeRouter } from '@/router'

const BOOT = {
  desk: { home: '/app', fallbacks: { incidents: '/app/wolf-incident' } },
  capabilities: { logs: { available: false, reason: 'No raw log lines are stored.' } },
}

async function render(component, path) {
  const router = makeRouter(createMemoryHistory('/wolf'))
  await router.push(path)
  await router.isReady()
  return mount(component, { global: { plugins: [router], provide: { boot: BOOT } } })
}

describe('PendingPage', () => {
  it('says the view is not built and links the Desk form of the record', async () => {
    const page = await render(PendingPage, '/incidents/WINC-00012')
    expect(page.text()).toContain('This view is not built yet')
    expect(page.text()).toContain('incident timeline')
    expect(page.get('a.wolf-link').attributes('href')).toBe('/app/wolf-incident/WINC-00012')
  })
})

describe('LogsPage', () => {
  it('explains why there is no log search and offers events instead', async () => {
    const page = await render(LogsPage, '/logs')
    expect(page.text()).toContain('Log search is not available. No raw log lines are stored.')
    expect(page.get('a.wolf-link').attributes('href')).toBe('/wolf/events')
    expect(page.find('input').exists()).toBe(false) // no fake search box
  })
})
