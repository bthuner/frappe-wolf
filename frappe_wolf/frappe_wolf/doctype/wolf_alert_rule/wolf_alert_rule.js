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
		if (!frm.is_new() && (frm.doc.channels || []).length) {
			frm.add_custom_button(__("Send test notification"), () =>
				frm.call("send_test_notification").then(() => frappe.show_alert(__("Test sent")))
			);
		}
	},
});
