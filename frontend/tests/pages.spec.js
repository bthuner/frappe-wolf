import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import LogsPage from '@/pages/LogsPage.vue'
import PendingPage from '@/pages/PendingPage.vue'
import { makeRouter } from '@/router'

const BOOT = {
  desk: { home: '/app', fallbacks: { incidents: '/app/wolf-incident' } },
  capabilities: { logs: { available: false, reason: 'No raw log lines are stored.' }, incidents: { available: true } },
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

describe('on a satellite', () => {
  it('explains where incidents live instead of linking a Desk list', async () => {
    const router = makeRouter(createMemoryHistory('/wolf'))
    await router.push('/incidents/WINC-00012')
    await router.isReady()
    const boot = { ...BOOT, capabilities: { ...BOOT.capabilities, incidents: { available: false } } }
    const page = mount(PendingPage, { global: { plugins: [router], provide: { boot } } })
    expect(page.text()).toContain('This site is a satellite')
    expect(page.find('a.wolf-link').exists()).toBe(false)
  })
})

describe('LogsPage with a log backend', () => {
  it('does not render an empty page', async () => {
    const router = makeRouter(createMemoryHistory('/wolf'))
    await router.push('/logs')
    await router.isReady()
    const boot = { ...BOOT, capabilities: { logs: { available: true } } }
    const page = mount(LogsPage, { global: { plugins: [router], provide: { boot } } })
    expect(page.text()).toContain('its view is not built yet')
  })
})
