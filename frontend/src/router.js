import { createRouter, createWebHistory } from 'vue-router'

const Pending = () => import('@/pages/PendingPage.vue')

// Every view of #19. `fallback` names the Desk page (boot.desk.fallbacks) that covers a view
// until it is built here; `planned` tells the reader what the view will show.
export const routes = [
  { path: '/', name: 'overview', component: Pending,
    meta: { title: 'Overview', fallback: 'overview', planned: 'Open incidents, unacknowledged and critical counts, sites with problems and monitoring freshness.' } },
  { path: '/incidents', name: 'incidents', component: Pending,
    meta: { title: 'Incidents', fallback: 'incidents', planned: 'Incidents you can see, by status, severity and scope.' } },
  { path: '/incidents/:incident', name: 'incident', component: Pending,
    meta: { title: 'Incident', fallback: 'incidents', deskDoc: { doctype: 'wolf-incident', param: 'incident' },
      planned: 'The incident timeline: evidence, context, operator activity and notifications on one rail.' } },
  { path: '/events', name: 'events', component: Pending,
    meta: { title: 'Events', fallback: 'events', planned: 'Every recorded event, filtered by type, severity, scope and time.' } },
  { path: '/errors', name: 'errors', component: Pending,
    meta: { title: 'Errors', fallback: 'errors', planned: 'Errors grouped by fingerprint, with first and last seen and affected sites.' } },
  { path: '/errors/:fingerprint', name: 'error-group', component: Pending,
    meta: { title: 'Error group', fallback: 'errors', planned: 'One error group: representative traceback, affected sites and linked incidents.' } },
  { path: '/metrics', name: 'metrics', component: Pending,
    meta: { title: 'Metrics', fallback: 'metrics', planned: 'Metric explorer with grouping, the resolution read and gaps kept as gaps.' } },
  { path: '/logs', name: 'logs', component: () => import('@/pages/LogsPage.vue'), meta: { title: 'Logs' } },
  { path: '/sites/:site', name: 'site', component: Pending,
    meta: { title: 'Site', fallback: 'overview', planned: 'One site: HTTP failures with deployments, jobs, backups, TLS and incidents.' } },
  // bench names repeat across hosts, so a bench is always addressed through its host
  { path: '/hosts/:host', name: 'host', component: Pending,
    meta: { title: 'Host', fallback: 'monitoring', planned: 'One host: CPU, memory, disk, load, its benches and its agent.' } },
  { path: '/hosts/:host/benches/:bench', name: 'bench', component: Pending,
    meta: { title: 'Bench', fallback: 'overview', planned: 'One bench on one host: workers, queues, Redis, scheduler and its sites.' } },
  { path: '/monitoring', name: 'monitoring', component: Pending,
    meta: { title: 'Monitoring', fallback: 'monitoring', planned: 'Is Wolf itself working: agents, satellites, clock skew, rule and scheduler freshness.' } },
  { path: '/:path(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'Not found' } },
]

export function makeRouter(history = createWebHistory('/wolf')) {
  const router = createRouter({ history, routes })
  router.afterEach((to) => {
    document.title = to.meta.title ? `${to.meta.title} · Frappe Wolf` : 'Frappe Wolf'
  })
  return router
}
