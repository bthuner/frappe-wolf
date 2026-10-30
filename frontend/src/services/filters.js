// Values the explorers accept, shared by the filter controls and the requests, so the URL,
// the controls and the data always agree. An unknown value in a URL falls back to the default.
export const RANGES = ['1h', '6h', '24h', '7d', '30d']
export const EVENT_TYPES = ['error', 'cron_failure', 'worker_failure', 'security', 'host_metric_alert', 'deployment']
export const SEVERITIES = ['info', 'warning', 'critical']

export function pick(value, allowed, fallback = undefined) {
  return allowed.includes(value) ? value : fallback
}
