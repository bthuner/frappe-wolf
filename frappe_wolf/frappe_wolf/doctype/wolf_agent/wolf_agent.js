// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.ui.form.on("Wolf Agent", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Rotate secret"), () =>
			frappe.confirm(
				__("The current secret stops working now. The new one is shown once and is not stored. Continue?"),
				() =>
					frm.call("rotate_secret").then((r) => {
						frm.reload_doc();
						const secret = frappe.utils.escape_html((r.message || {}).agent_secret || "");
						const where =
							frm.doc.kind === "satellite"
								? __("Register the satellite site again (Wolf Settings → Register with central site); it cannot take a pasted secret.")
								: __("Set it as <code>agent_secret</code> in <code>/etc/wolf-agent/config.json</code> on the host and restart <code>wolf-agent</code>, or run <code>wolf-agent register</code> again.");
						frappe.msgprint({
							title: __("New agent secret"),
							indicator: "orange",
							message: `<p>${__("Copy it now: Wolf keeps only a hash and cannot show it again.")}</p>
								<pre style="user-select: all">${secret}</pre><p>${where}</p>`,
						});
					})
			)
		);
	},
});
