// The Wolf API as the browser tests see it: every /api/method/<method> call is answered in the
// page from the fixtures below (or a test's overrides), so the real production build runs with
// no Frappe behind it. Calls are recorded for the tests to assert on.
const NOW = new Date()
const ago = (minutes) => new Date(NOW.getTime() - minutes * 60000).toISOString()

export const BOOT = {
  user: { name: 'ops@example.com', full_name: 'Ops' },
  can: { read: true, comment: true, act: true, configure: true },
  capabilities: {
    logs: { available: false, reason: 'Wolf stores parsed events and metrics, not raw log lines, so there is nothing to search yet.' },
    metrics: { available: true, resolutions: ['raw', '1m', '1h'] },
    incidents: { available: true },
    realtime: { available: false, reason: 'Live updates need Frappe\'s socket.io server; views refresh on demand.' },
    restricted_scope: false,
  },
  time_zone: 'Europe/Paris',
  server_version: '0.1.0',
  satellite: false,
  desk: { home: '/app', fallbacks: { alert_rules: '/app/wolf-alert-rule', settings: '/app/wolf-settings' } },
}

export const INCIDENT = {
  name: 'WINC-00001', title: '502 spike after deployment: a.example.com', status: 'Open', severity: 'critical',
  scope: { site: 'a.example.com', bench: 'frappe-bench', host: 'bench-01' }, summary: '14 x 502',
  started_at: ago(50), last_event_at: ago(40), acknowledged: { at: null }, resolved: { at: null }, assigned_to: null,
  alert_rule: { name: '502 spike after deployment', readable: true },
  deployment: { at: ago(52), event: 'E0', available: true }, silenced: false, notified_at: ago(49), escalated_at: null,
  merged_into: null, split_from: null, postmortem: null, event_records: 2, event_count: 15, time_zone: 'Europe/Paris',
  timeline_window: { since: ago(80), until: NOW.toISOString(), padding_minutes: 30, open_ended: true },
  regulatory: null,
  can: { comment: true, acknowledge: true, resolve: true, assign_to_me: true, workflow: true },
}  // prettier-ignore

const ROWS = [
  { id: 'event:E0', kind: 'deployment', family: 'context', relation: 'preceding_change', at: ago(52), summary: 'bench update',
    scope: { site: 'a.example.com' }, count: 1, details: { task: 'update' } },
  { id: 'event:E1', kind: 'event', family: 'evidence', relation: 'linked', at: ago(50), severity: 'critical',
    summary: '502 on GET /api/method/frappe.auth.get_logged_user', scope: { site: 'a.example.com' }, count: 14, last_seen: ago(45), details: {} },
  { id: 'activity:comment:c1', kind: 'activity', family: 'activity', activity: 'comment', at: ago(30), summary: 'Ops commented',
    user_name: 'Ops', text: 'restarting workers' },
]  // prettier-ignore

const METRIC = { metric: 'disk_used_percent', label: 'Disk used', unit: '%', kind: 'gauge', seen: true, label_key: 'mount',
  aggregations: ['avg', 'min', 'max', 'last'], default_aggregation: 'avg', group_bys: ['none', 'host', 'host_label'] }  // prettier-ignore

export function fixtures() {
  return {
    'frappe_wolf.api.spa.get_boot': () => BOOT,
    'frappe_wolf.api.scope.get_counts': () => ({
      restricted: false,
      incidents: { unresolved: 3, critical_unresolved: 1, unacknowledged: 2, sites_with_unresolved: 2, unresolved_without_site: 0 },
      agents: { agent: { fresh: 2, stale: 0, never_seen: 0, disabled: 0 }, satellite: { fresh: 0, stale: 0, never_seen: 0, disabled: 0 } },
    }),
    'frappe_wolf.api.dashboard.get_http': () => ({
      window: { since: ago(60), until: NOW.toISOString() }, bucket_seconds: 60, resolution: '1m', deployments: [],
      series: { http_requests: [[ago(2), 120], [ago(1), 118]], http_5xx: [[ago(2), 3], [ago(1), 0]], http_502: [[ago(2), 2], [ago(1), 0]] },
      has_data: { http_requests: true, http_5xx: true, http_502: true },
    }),
    'frappe_wolf.api.dashboard.get_signals': () => ({
      series: [{ metric_name: 'disk_used_percent', group: 'host', label: 'Disk used', unit: '%', host: 'bench-01', label_key: 'mount=/',
        value: 93, last_at: ago(1), state: 'firing', rules: [], incidents: ['WINC-00002'] }],
      counts: { host: { firing: 1 } }, missing: [], caveats: {}, email: {}, lookback_days: 7, truncated: false, total_series: 1,
    }),
    'frappe_wolf.api.incident_view.list_incidents': () => ({
      incidents: [{ name: INCIDENT.name, title: INCIDENT.title, status: 'Open', severity: 'critical', site: 'a.example.com',
        bench: 'frappe-bench', host: 'bench-01', started_at: INCIDENT.started_at, resolved_at: null, merged_into: null, assigned_name: null }],
      paging: { has_more: false, next_cursor: null },
    }),
    'frappe_wolf.api.scope.get_inventory': () => ({ hosts: [], note: '' }),
    'frappe_wolf.api.dashboard.get_monitoring': () => ({ health: null, rollups: {}, agents: [], silence_minutes: 5, mode: { mode: 'central' }, note: '' }),
    'frappe_wolf.api.incident_view.get_incident': () => INCIDENT,
    'frappe_wolf.api.incident_view.get_timeline': () => ({ rows: ROWS, paging: { has_more: false, next_cursor: null }, totals: {}, truncated: {}, window: null }),
    'frappe_wolf.api.incident_view.act': () => ({ ...INCIDENT, status: 'Acknowledged', acknowledged: { at: NOW.toISOString(), by: 'ops@example.com', by_name: 'Ops' },
      can: { ...INCIDENT.can, acknowledge: false } }),
    'frappe_wolf.api.response.get_response': () => ({ proposals: [], actions: [], can_approve: true, default_hours: 24, max_hours: 168 }),
    'frappe_wolf.api.explore.list_events': () => ({
      events: [{ name: 'E1', title: 'KeyError: customer', event_type: 'error', severity: 'critical', count: 14, first_seen: ago(30),
        last_seen: ago(5), site: 'a.example.com', bench: 'frappe-bench', host: 'bench-01', incident: null, fingerprint: 'fp1' }],
      paging: { has_more: false },
    }),
    'frappe_wolf.api.explore.list_error_groups': () => ({
      groups: [{ fingerprint: 'fp1', title: 'KeyError: customer', severity: 'critical', records: 8, records_in_window: 3, occurrences: 47,
        sites: 2, host_wide: false, incidents: 1, first_seen: ago(300), last_seen: ago(5), latest: { site: 'a.example.com' } }],
      paging: { has_more: false },
    }),
    'frappe_wolf.api.metrics.get_metric_catalog': () => ({ metrics: [METRIC] }),
    'frappe_wolf.api.metrics.get_labels': () => ({ labels: ['mount=/'] }),
    'frappe_wolf.api.metrics.query_metric': () => ({
      metric: METRIC, aggregation: 'avg', resolution: 'raw', bucket_seconds: 60, window: { since: ago(60), until: NOW.toISOString() },
      retention: { clipped: false }, groups: { shown: 1, total: 1, ranked_by: 'max over the window, highest first' }, live: true,
      series: [{ name: 'bench-01 · mount=/', value: 93, samples: 60, last_sample: ago(1), stale: false, points: [[ago(2), 92], [ago(1), 93]] }],
    }),
    'frappe_wolf.api.security.list_sites': () => ['a.example.com'],
    'frappe_wolf.api.security.get_posture': () => ({
      site: 'a.example.com', local: true, hosts: ['bench-01'], recent_hours: 3, score: 71, coverage: 100, passed: 6, failed: 2, unknown: 0,
      checks: [
        { id: 'two_factor', label: 'Two-factor authentication for System Managers', weight: 3, status: 'fail', evidence: 'disabled in System Settings',
          fix: 'System Settings → Enable Two Factor Auth.' },
        { id: 'backup', label: 'A backup newer than 26 h', weight: 2, status: 'pass', evidence: 'newest backup 3.0 h old', fix: null },
      ],
    }),
    'frappe_wolf.api.journal.get_status': () => ({ entries: 12, anchor: `12:${'a'.repeat(64)}`, pending: 0, key: true }),
  }
}

// Answer every API call of `page` from the fixtures (and `overrides`); returns the call log.
export async function mockApi(page, overrides = {}) {
  const handlers = { ...fixtures(), ...overrides }
  const calls = []
  await page.route('**/socket.io/**', (route) => route.abort())
  await page.route('**/api/method/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = url.pathname.split('/api/method/')[1]
    const body = request.method() === 'POST' ? request.postDataJSON() : Object.fromEntries(url.searchParams)
    const handler = handlers[method]
    calls.push({ method, args: body || {}, mocked: !!handler })
    if (!handler) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ exception: `frappe.DoesNotExistError: ${method} not mocked` }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ message: await handler(body) }) })
  })
  return calls
}
