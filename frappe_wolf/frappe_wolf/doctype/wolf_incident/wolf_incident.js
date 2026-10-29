// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Incident", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.status === "Open") {
			frm.add_custom_button(__("Acknowledge"), () => frm.call("acknowledge").then(() => frm.reload_doc()));
		}
		if (frm.doc.status !== "Resolved") {
			frm.add_custom_button(__("Resolve"), () => frm.call("resolve").then(() => frm.reload_doc()));
		}
		frm.add_custom_button(__("Overview"), () => {
			frappe.route_options = {};
			frappe.set_route("wolf-overview");
		});
		frm.trigger("render_timeline");
		frm.trigger("render_deliveries");
	},

	render_deliveries(frm) {
		const $wrap = frm.get_field("deliveries_html").$wrapper;
		const esc = frappe.wolf.esc;
		frappe
			.call("frappe_wolf.api.ui.get_incident_deliveries", { incident: frm.doc.name })
			.then(({ message }) => {
				if (!message.length) {
					$wrap.html(`<p class="text-muted">${__("No notification sent for this incident.")}</p>`);
					return;
				}
				const rows = message
					.map(
						(d) => `<tr>
							<td>${esc(frappe.datetime.str_to_user(d.at))}</td>
							<td>${esc(d.kind)}${d.digest ? ` <span class="text-muted">(${__("digest")})</span>` : ""}</td>
							<td>${esc(d.channel_type)}</td><td>${esc(d.target)}</td><td>${esc(d.action || "")}</td>
							<td>${d.ok ? `<span class="indicator-pill green">${__("ok")}</span>` : `<span class="indicator-pill red">${__("failed")}</span>`}</td>
							<td>${esc(d.http_status || "")}</td><td class="text-muted">${esc(d.error || "")}</td></tr>`
					)
					.join("");
				$wrap.html(
					`<div class="wolf-card"><table class="table table-sm"><thead><tr><th>${__("At")}</th><th>${__("Kind")}</th>
					<th>${__("Channel")}</th><th>${__("Target")}</th><th>${__("Action")}</th><th></th><th>${__("HTTP")}</th>
					<th>${__("Error")}</th></tr></thead><tbody>${rows}</tbody></table></div>`
				);
			});
	},

	render_timeline(frm) {
		const $wrap = frm.get_field("timeline_html").$wrapper;
		$wrap.html(`<p class="text-muted">${__("Loading timeline…")}</p>`);
		frappe
			.call("frappe_wolf.api.ui.get_incident_timeline", { incident: frm.doc.name })
			.then(({ message }) => {
				$wrap.html(
					`<div class="wolf-card">${frappe.wolf.render_timeline(message.events, {
						ascending: true,
						hide_incident: true,
					})}</div>`
				);
			});
	},
});
