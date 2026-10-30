import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import LogsPage from '@/pages/LogsPage.vue'
import PendingPage from '@/pages/PendingPage.vue'
import { makeRouter } from '@/router'

const BOOT = {
  desk: { home: '/app', fallbacks: { incidents: '/app/wolf-incident', metrics: '/app/wolf-overview' } },
  capabilities: { logs: { available: false, reason: 'No raw log lines are stored.' }, incidents: { available: true } },
}

async function render(component, path) {
  const router = makeRouter(createMemoryHistory('/wolf'))
  await router.push(path)
  await router.isReady()
  return mount(component, { global: { plugins: [router], provide: { boot: BOOT } } })
}

describe('PendingPage', () => {
  it('says a view is not built and links the Desk page that covers it', async () => {
    const { createRouter } = await import('vue-router')
    const router = createRouter({
      history: createMemoryHistory('/wolf'),
      routes: [{ path: '/later', component: PendingPage, meta: { title: 'Later', fallback: 'metrics', planned: 'Something useful.' } }],
    })
    await router.push('/later')
    await router.isReady()
    const page = mount(PendingPage, { global: { plugins: [router], provide: { boot: BOOT } } })
    expect(page.text()).toContain('This view is not built yet')
    expect(page.text()).toContain('Something useful.')
    expect(page.get('a.wolf-link').attributes('href')).toBe('/app/wolf-overview')
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
  it('sends incident links to an explanation instead of an empty page', async () => {
    const boot = { ...BOOT, capabilities: { ...BOOT.capabilities, incidents: { available: false } } }
    const router = makeRouter(createMemoryHistory('/wolf'), boot)
    await router.push('/incidents/WINC-00012')
    await router.isReady()
    expect(router.currentRoute.value.name).toBe('unavailable')
    expect(router.currentRoute.value.query.from).toBe('/incidents/WINC-00012')
    const page = mount(PendingPage, { global: { plugins: [router], provide: { boot } } })
    expect(page.text()).toContain('This site is a satellite')
    expect(page.text()).toContain('Look for incidents there.')
    expect(page.text()).not.toContain('incidents,WINC') // no array of path segments in the title
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
