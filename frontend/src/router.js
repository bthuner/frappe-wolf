import { createRouter, createWebHistory } from 'vue-router'

const Pending = () => import('@/pages/PendingPage.vue')

// Every view of #19. `fallback` names the Desk page (boot.desk.fallbacks) that covers a view
// until it is built here; `planned` tells the reader what the view will show.
export const routes = [
  { path: '/', name: 'overview', component: () => import('@/pages/DashboardPage.vue'), meta: { title: 'Overview' } },
  { path: '/incidents', name: 'incidents', component: () => import('@/pages/IncidentsPage.vue'),
    meta: { title: 'Incidents', capability: 'incidents' } },
  { path: '/incidents/:incident', name: 'incident', component: () => import('@/pages/IncidentPage.vue'),
    meta: { title: 'Incident', capability: 'incidents' } },
  { path: '/events', name: 'events', component: () => import('@/pages/EventsPage.vue'), meta: { title: 'Events' } },
  { path: '/errors', name: 'errors', component: () => import('@/pages/ErrorsPage.vue'), meta: { title: 'Errors' } },
  { path: '/errors/:fingerprint', name: 'error-group', component: () => import('@/pages/ErrorGroupPage.vue'), meta: { title: 'Error group' } },
  { path: '/metrics', name: 'metrics', component: () => import('@/pages/MetricsPage.vue'), meta: { title: 'Metrics' } },
  { path: '/logs', name: 'logs', component: () => import('@/pages/LogsPage.vue'), meta: { title: 'Logs' } },
  { path: '/sites/:site', name: 'site', component: () => import('@/pages/DashboardPage.vue'), meta: { title: 'Site' } },
  { path: '/hosts/:host', name: 'host', component: () => import('@/pages/DashboardPage.vue'), meta: { title: 'Host' } },
  // bench names repeat across hosts, so a bench is always addressed through its host
  { path: '/hosts/:host/benches/:bench', name: 'bench', component: () => import('@/pages/DashboardPage.vue'), meta: { title: 'Bench' } },
  { path: '/monitoring', name: 'monitoring', component: () => import('@/pages/MonitoringPage.vue'), meta: { title: 'Monitoring' } },
  // where a view this site cannot have leads (incidents on a satellite); ?from= keeps the link
  { path: '/unavailable/:path(.*)*', name: 'unavailable', component: Pending,
    meta: { title: 'Incidents', capability: 'incidents', noSubject: true } },
  { path: '/:path(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'Not found' } },
]

export function makeRouter(history = createWebHistory('/wolf'), boot = null) {
  const router = createRouter({ history, routes })
  // a view that needs a backend this site lacks (incidents on a satellite) explains instead
  router.beforeEach((to) => {
    const cap = to.meta.capability
    if (cap && boot && to.name !== 'unavailable' && !boot.capabilities?.[cap]?.available) {
      return { name: 'unavailable', params: { path: to.path.split('/').filter(Boolean) }, query: { from: to.fullPath } }
    }
  })
  router.afterEach((to) => {
    document.title = to.meta.title ? `${to.meta.title} · Frappe Wolf` : 'Frappe Wolf'
  })
  return router
}
