// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Alert Rule", {
	refresh(frm) {
		frm.add_custom_button(__("Preview"), () =>
			frm.call("preview").then(({ message }) => {
				const rows = message.groups
					.map(
						(g) =>
							`<tr><td>${frappe.wolf.esc(g.group)}</td><td>${frappe.wolf.esc(g.value)}</td>
							<td>${frappe.wolf.esc(g.samples)}</td>
							<td>${g.breached ? `<span class="indicator-pill red">${__("breached")}</span>` : ""}</td></tr>`
					)
					.join("");
				frappe.msgprint({
					title: __("Current values"),
					message:
						(message.active ? "" : `<p class="text-muted">${__("Outside the active window right now.")}</p>`) +
						(rows
							? `<table class="table table-sm"><thead><tr><th>${__("Group")}</th><th>${__("Value")}</th><th>${__("Samples")}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
							: __("No data in the window.")),
					wide: true,
				});
			})
		);
		if (!frm.is_new() && (frm.doc.channels || []).concat(frm.doc.escalation_channels || []).length) {
			frm.add_custom_button(__("Send test notification"), () =>
				frm.call("send_test_notification").then(({ message }) => {
					const esc = frappe.wolf.esc;
					const rows = (message.results || [])
						.map(
							(r) =>
								`<tr><td>${esc(r.channel_type)}</td><td>${esc(r.target)}</td><td>${esc(r.action || "")}</td>
								<td>${r.ok ? `<span class="indicator-pill green">${__("sent")}</span>` : `<span class="indicator-pill red">${__("failed")}</span>`}</td>
								<td>${esc(r.http_status || "")}</td><td>${esc(r.error || "")}</td></tr>`
						)
						.join("");
					frappe.msgprint({
						title: __("Test notification"),
						message: rows
							? `<table class="table table-sm"><thead><tr><th>${__("Channel")}</th><th>${__("Target")}</th><th>${__("Action")}</th><th></th><th>${__("HTTP")}</th><th>${__("Error")}</th></tr></thead><tbody>${rows}</tbody></table>
							<p class="text-muted">${__("Recorded on incident {0}.", [`<a href="/app/wolf-incident/${encodeURIComponent(message.incident)}">${esc(message.incident)}</a>`])}</p>`
							: __("No enabled channel to test."),
						wide: true,
					});
				})
			);
		}
	},
});
