frappe.pages["wolf-overview"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Wolf Overview"),
		single_column: true,
	});
	wrapper.wolf = new WolfOverview(page);
};

frappe.pages["wolf-overview"].on_page_show = function (wrapper) {
	wrapper.wolf && wrapper.wolf.refresh();
};

const esc = frappe.wolf.esc;

class WolfOverview {
	constructor(page) {
		this.page = page;
		this.state = { range: "24h", site: "", bench: "", host: "" };
		this.make_filters();
		this.$body = $(`<div class="wolf-overview"></div>`).appendTo(page.main);
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_inner_button(__("Alert Rules"), () => frappe.set_route("List", "Wolf Alert Rule"));
		this.page.add_inner_button(__("Agents"), () => frappe.set_route("List", "Wolf Agent"));
		this.timer = setInterval(() => {
			if (frappe.get_route_str() === "wolf-overview" && !document.hidden) this.refresh();
		}, 30000);
	}

	make_filters() {
		this.fields = {};
		this.fields.range = this.page.add_field({
			fieldname: "range",
			label: __("Range"),
			fieldtype: "Select",
			options: ["1h", "6h", "24h", "7d"],
			default: "24h",
			change: () => this.set_filter("range"),
		});
		for (const f of ["site", "bench", "host"]) {
			this.fields[f] = this.page.add_field({
				fieldname: f,
				label: __(frappe.utils.to_title_case(f)),
				fieldtype: "Autocomplete",
				options: [],
				change: () => this.set_filter(f),
			});
		}
	}

	set_filter(f) {
		const value = this.fields[f].get_value() || "";
		if (this.state[f] === value) return;
		this.state[f] = value;
		this.refresh();
	}

	async refresh() {
		if (this.loading) return;
		this.loading = true;
		try {
			const { message } = await frappe.call({
				method: "frappe_wolf.api.ui.get_overview",
				args: this.state,
			});
			this.data = message;
			if (message.wolf && message.wolf.mode === "satellite") {
				this.render_satellite(message.wolf);
				return;
			}
			this.update_scope_options(message.scopes);
			this.render();
			this.render_metrics();
		} finally {
			this.loading = false;
		}
	}

	update_scope_options(scopes) {
		for (const f of ["site", "bench", "host"]) {
			this.fields[f].set_data((scopes[f] || []).map((v) => ({ label: v, value: v })));
		}
	}

	total(type, severity) {
		return (this.data.counts || [])
			.filter((c) => (!type || c.event_type === type) && (!severity || c.severity === severity))
			.reduce((sum, c) => sum + (c.total || 0), 0);
	}

	render() {
		const d = this.data;
		const online = d.agents.filter((a) => a.online).length;
		const stats = [
			[__("Open incidents"), d.open_incidents.length, d.open_incidents.length ? "red" : "green"],
			[__("Critical events"), this.total(null, "critical"), this.total(null, "critical") ? "red" : "green"],
			[__("Cron failures"), this.total("cron_failure"), this.total("cron_failure") ? "orange" : "green"],
			[__("Security events"), this.total("security"), this.total("security") ? "orange" : "green"],
			[__("Agents online"), `${online}/${d.agents.length}`, online === d.agents.length ? "green" : "orange"],
		];
		this.$body.html(`
			<div class="wolf-stats">
				${stats
					.map(
						([label, value, color]) => `
					<div class="wolf-stat">
						<div class="wolf-stat-value text-${color}">${esc(value)}</div>
						<div class="wolf-stat-label">${esc(label)}</div>
					</div>`
					)
					.join("")}
			</div>
			<div class="wolf-grid">
				<section class="wolf-card">
					<h4>${__("Open incidents")}</h4>
					${this.render_incidents(d.open_incidents)}
				</section>
				<section class="wolf-card">
					<h4>${__("Agents")}</h4>
					${this.render_agents(d.agents)}
				</section>
			</div>
			<section class="wolf-card">
				<h4>${__("Timeline")} <span class="text-muted small">${esc(this.state.range)}</span></h4>
				${frappe.wolf.render_timeline(d.events)}
			</section>
			<section class="wolf-card wolf-metrics">
				<h4>${__("Host metrics")} <span class="text-muted small wolf-metrics-host"></span></h4>
				<div class="wolf-charts"></div>
			</section>
		`);
	}

	render_satellite(w) {
		const central = w.central_url
			? `<a href="${esc(w.central_url)}/app/wolf-overview" target="_blank">${esc(w.central_url)}</a>`
			: `<em>${__("no central site yet")}</em>`;
		const ok = w.last_forward_status === "ok";
		const rows = [
			[__("Satellite ID"), esc(w.satellite_id || "-")],
			[__("Pending events"), esc(w.pending)],
			[__("Last forward"), w.last_forward_at ? frappe.datetime.comment_when(w.last_forward_at) : "-"],
			[
				__("Status"),
				`<span class="indicator-pill ${ok ? "green" : "orange"}">${esc(w.last_forward_status || __("never forwarded"))}</span>`,
			],
		];
		if (w.forward_retry_at) rows.push([__("Next retry"), frappe.datetime.comment_when(w.forward_retry_at)]);
		if (w.last_forward_error) rows.push([__("Last error"), `<code>${esc(w.last_forward_error)}</code>`]);
		this.$body.html(`
			<section class="wolf-card wolf-satellite">
				<h4>${__("This site is a satellite of {0}", [central])}</h4>
				<p class="text-muted">${__(
					"Scheduler failures, errors, logins, privileged role grants and migrations on this site are forwarded to the central Wolf site. Incidents, rules and the timeline live there."
				)}</p>
				<table class="table table-sm">
					${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}
				</table>
				<a href="/app/wolf-settings">${__("Wolf Settings")}</a>
				· <a href="/app/wolf-outbox">${__("Outbox")}</a>
			</section>
		`);
	}

	render_incidents(rows) {
		if (!rows.length) return `<p class="text-muted">${__("Nothing open. The pack is quiet.")}</p>`;
		return `<ul class="wolf-list">${rows
			.map(
				(i) => `
			<li>
				<span class="wolf-sev wolf-sev-${esc(i.severity)}"></span>
				<a href="/app/wolf-incident/${encodeURIComponent(i.name)}">${esc(i.title)}</a>
				<span class="indicator-pill ${i.status === "Open" ? "red" : "orange"}">${__(i.status)}</span>
				<div class="text-muted small">${esc([i.site, i.bench, i.host].filter(Boolean).join(" · "))}
					· ${frappe.datetime.comment_when(i.started_at)} · ${esc(i.event_count || 0)} ${__("events")}</div>
			</li>`
			)
			.join("")}</ul>`;
	}

	render_agents(rows) {
		if (!rows.length) {
			return `<p class="text-muted">${__(
				"No agent registered yet. Get the install command from Wolf Settings."
			)}</p>`;
		}
		return `<ul class="wolf-list">${rows
			.map(
				(a) => `
			<li>
				<span class="indicator-pill ${a.online ? "green" : "red"}">${a.online ? __("online") : __("silent")}</span>
				${
					a.kind === "satellite"
						? `<a href="/app/wolf-agent/${encodeURIComponent(a.name)}">${esc(a.hostname)}</a> <span class="badge">${__("satellite")}</span>`
						: `<a href="#" class="wolf-host" data-host="${esc(a.hostname)}">${esc(a.hostname)}</a>`
				}
				<span class="text-muted small">${a.last_seen ? frappe.datetime.comment_when(a.last_seen) : ""}</span>
			</li>`
			)
			.join("")}</ul>`;
	}

	async render_metrics() {
		const $wrap = this.$body.find(".wolf-charts");
		this.$body.find(".wolf-host").on("click", (e) => {
			e.preventDefault();
			this.fields.host.set_value($(e.currentTarget).data("host"));
		});
		const first = this.data.agents.find((a) => a.kind !== "satellite");
		const host = this.state.host || (first && first.hostname);
		if (!host) {
			$wrap.html(`<p class="text-muted">${__("No host metrics yet.")}</p>`);
			return;
		}
		this.$body.find(".wolf-metrics-host").text(host);
		const range = this.state.range === "7d" ? "7d" : this.state.range === "24h" ? "24h" : "6h";
		const { message } = await frappe.call({
			method: "frappe_wolf.api.ui.get_metric_series",
			args: { host, range },
		});
		const groups = {};
		for (const [key, points] of Object.entries(message.series)) {
			const metric = key.split(" [")[0];
			(groups[metric] = groups[metric] || []).push([key, points]);
		}
		if (!Object.keys(groups).length) {
			$wrap.html(`<p class="text-muted">${__("No samples for {0} in this range.", [esc(host)])}</p>`);
			return;
		}
		$wrap.empty();
		for (const [metric, series] of Object.entries(groups)) {
			const $c = $(`<div class="wolf-chart"><div class="wolf-chart-title">${esc(metric)}</div><div></div></div>`).appendTo($wrap);
			const labels = series[0][1].map(([t]) => (range === "7d" ? t.slice(5, 16) : t.slice(11, 16)));
			new frappe.Chart($c.children().get(1), {
				type: "line",
				height: 160,
				data: {
					labels,
					datasets: series.map(([name, points]) => ({ name, values: points.map(([, v]) => v) })),
				},
				lineOptions: { hideDots: 1, regionFill: 1 },
				axisOptions: { xIsSeries: 1, xAxisMode: "tick" },
			});
		}
	}
}
