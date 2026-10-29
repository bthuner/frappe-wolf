// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Settings", {
	refresh(frm) {
		if (frm.doc.mode === "satellite") {
			frm.dashboard.set_headline(
				frm.doc.central_url
					? __("This site is a satellite of {0}: its signals are forwarded there.", [
							`<a href="${frappe.wolf.esc(frm.doc.central_url)}/app/wolf-overview" target="_blank">${frappe.wolf.esc(frm.doc.central_url)}</a>`,
					  ])
					: __("Satellite mode: register with the central site to start forwarding."),
				"blue"
			);
			frm.add_custom_button(__("Forward now"), () =>
				frappe
					.call({ method: "frappe_wolf.satellite.forward_now", freeze: true })
					.then(({ message }) => {
						frappe.show_alert({
							message: message.error
								? __("Forwarding: {0}", [message.error])
								: __("Forwarded {0} events", [message.sent]),
							indicator: message.status === "ok" ? "green" : "orange",
						});
						frm.reload_doc();
					})
			);
		} else {
			frm.add_custom_button(__("Agent install command"), () =>
				frm.call("get_install_command").then(({ message }) => {
					frappe.msgprint({
						title: __("Install the Wolf agent"),
						message: `<p>${__("Run on each host, as root:")}</p><pre style="white-space: pre-wrap">${frappe.wolf.esc(message)}</pre>`,
						wide: true,
					});
				})
			);
			frm.add_custom_button(__("Regenerate token"), () =>
				frappe.confirm(__("Agents already registered keep working. New registrations need the new token. Continue?"), () =>
					frm.call("regenerate_registration_token").then(() => frm.reload_doc())
				)
			);
		}
		frm.add_custom_button(__("Register with central site"), () => register_dialog(frm));
	},
});

function register_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Register with central site"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"This site becomes a satellite: it keeps capturing its scheduler failures, errors, logins and migrations, and forwards them to the central Wolf site instead of storing them."
				)}</p>`,
			},
			{ fieldname: "url", fieldtype: "Data", label: __("Central URL"), options: "URL", reqd: 1, default: frm.doc.central_url },
			{
				fieldname: "token",
				fieldtype: "Password",
				label: __("Registration Token"),
				reqd: 1,
				description: __("From Wolf Settings on the central site."),
			},
			{ fieldname: "verify_tls", fieldtype: "Check", label: __("Verify TLS"), default: frm.doc.mode === "satellite" ? frm.doc.verify_tls : 1 },
		],
		primary_action_label: __("Register"),
		primary_action(values) {
			frappe
				.call({ method: "frappe_wolf.satellite.register", args: values, freeze: true })
				.then(({ message }) => {
					d.hide();
					frappe.show_alert({ message: __("Registered as {0}", [message.satellite_id]), indicator: "green" });
					frm.reload_doc();
				});
		},
	});
	d.show();
}
