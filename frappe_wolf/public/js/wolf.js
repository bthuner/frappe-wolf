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

frappe.wolf.ACTIVITY_ICON = { opened: "●", status: "◆", field: "✎", comment: "“", note: "i" };

frappe.wolf.render_activity = function (a) {
	const esc = frappe.wolf.esc;
	const who = a.user_name || a.user;
	// a.content of a comment is sanitized on the server (clean_html); notes are plain text
	const body =
		a.kind === "comment"
			? `<div><strong>${esc(who || "")}</strong> <span class="text-muted">${__("commented")}</span></div>
				<div class="wolf-comment">${a.content || ""}</div>`
			: a.kind === "note"
			? `<div class="text-muted">${esc(a.content)}</div>`
			: `<div>${esc(a.text)}</div>`;
	return `<li class="wolf-timeline-item wolf-activity wolf-activity-${esc(a.kind)}">
		<span class="wolf-time">${esc((a.timestamp || "").slice(11, 16))}</span>
		<span class="wolf-activity-icon" aria-hidden="true">${esc(frappe.wolf.ACTIVITY_ICON[a.kind] || "•")}</span>
		<div class="wolf-timeline-body">${body}</div>
	</li>`;
};

frappe.wolf.render_event = function (e, opts) {
	const esc = frappe.wolf.esc;
	const scope = [e.site, e.bench, e.host].filter(Boolean).join(" · ");
	const count = e.count > 1 ? ` <span class="badge">×${esc(e.count)}</span>` : "";
	return `<li class="wolf-timeline-item wolf-type-${esc(e.event_type)} ${e.linked ? "wolf-linked" : ""}">
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
};

// events: Wolf Event rows. opts.activity: entries from get_incident_timeline (comments,
// assignments, status changes), interleaved with the events by timestamp.
frappe.wolf.render_timeline = function (events, opts = {}) {
	const esc = frappe.wolf.esc;
	const items = [
		...(events || []).map((e) => ({ ts: e.timestamp || "", html: () => frappe.wolf.render_event(e, opts) })),
		...(opts.activity || []).map((a) => ({ ts: a.timestamp || "", html: () => frappe.wolf.render_activity(a) })),
	];
	if (!items.length) return `<p class="text-muted">${__("No events in this range.")}</p>`;
	items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
	if (!opts.ascending) items.reverse();
	let last_day = null;
	return `<ol class="wolf-timeline">${items
		.map((item) => {
			const day = item.ts.slice(0, 10);
			const header =
				day !== last_day
					? `<li class="wolf-timeline-day">${esc(frappe.datetime.str_to_user(day))}</li>`
					: "";
			last_day = day;
			return header + item.html();
		})
		.join("")}</ol>`;
};
