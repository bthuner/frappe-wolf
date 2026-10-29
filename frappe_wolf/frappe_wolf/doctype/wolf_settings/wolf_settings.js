// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Settings", {
	refresh(frm) {
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
	},
});
