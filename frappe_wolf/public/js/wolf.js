frappe.provide("frappe.wolf");

frappe.wolf.esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
frappe.wolf.TYPE_LABEL = {
	error: __("Error"),
	cron_failure: __("Cron failure"),
	worker_failure: __("Worker failure"),
	security: __("Security"),
	host_metric_alert: __("Metric alert"),
	deployment: __("Deployment"),
};

frappe.wolf.render_timeline = function (events, opts = {}) {
	const esc = frappe.wolf.esc;
	if (!events || !events.length) return `<p class="text-muted">${__("No events in this range.")}</p>`;
	const rows = [...events].sort((a, b) => (a.timestamp < b.timestamp ? -1 : 1));
	if (!opts.ascending) rows.reverse();
	let last_day = null;
	return `<ol class="wolf-timeline">${rows
		.map((e) => {
			const day = (e.timestamp || "").slice(0, 10);
			const header =
				day !== last_day
					? `<li class="wolf-timeline-day">${esc(frappe.datetime.str_to_user(day))}</li>`
					: "";
			last_day = day;
			const scope = [e.site, e.bench, e.host].filter(Boolean).join(" · ");
			const count = e.count > 1 ? ` <span class="badge">×${esc(e.count)}</span>` : "";
			return `${header}
			<li class="wolf-timeline-item wolf-type-${esc(e.event_type)} ${e.linked ? "wolf-linked" : ""}">
				<span class="wolf-time">${esc((e.timestamp || "").slice(11, 16))}</span>
				<span class="wolf-sev wolf-sev-${esc(e.severity)}" title="${esc(e.severity)}"></span>
				<div class="wolf-timeline-body">
					<a href="/app/wolf-event/${encodeURIComponent(e.name)}">${esc(e.title)}</a>${count}
					<div class="text-muted small">
						<span class="wolf-type">${esc(frappe.wolf.TYPE_LABEL[e.event_type] || e.event_type)}</span>
						${e.category ? ` · ${esc(e.category)}` : ""} · ${esc(scope)}
						${e.incident && !opts.hide_incident ? ` · <a href="/app/wolf-incident/${encodeURIComponent(e.incident)}">${esc(e.incident)}</a>` : ""}
					</div>
				</div>
			</li>`;
		})
		.join("")}</ol>`;
};
