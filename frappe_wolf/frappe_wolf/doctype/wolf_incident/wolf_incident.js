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
