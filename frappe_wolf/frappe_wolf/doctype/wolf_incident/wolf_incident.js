// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

const WOLF_API = "frappe_wolf.api.incident";

frappe.ui.form.on("Wolf Incident", {
	refresh(frm) {
		if (frm.is_new()) return;
		const manager = frappe.user.has_role(["Wolf Manager", "System Manager"]);
		const merged = !!frm.doc.merged_into;

		if (merged) {
			frm.set_intro(
				__("Merged into {0}.", [
					`<a href="/app/wolf-incident/${encodeURIComponent(frm.doc.merged_into)}">${frappe.wolf.esc(frm.doc.merged_into)}</a>`,
				]),
				"blue"
			);
		}
		if (frm.doc.status === "Open") {
			frm.add_custom_button(__("Acknowledge"), () => frm.call("acknowledge").then(() => frm.reload_doc()));
		}
		if (frm.doc.status !== "Resolved") {
			frm.add_custom_button(__("Resolve"), () => frm.call("resolve").then(() => frm.reload_doc()));
		}
		if (manager && !merged && frm.doc.status !== "Resolved" && frm.doc.assigned_to !== frappe.session.user) {
			frm.add_custom_button(__("Assign to me"), () => frm.call("assign_to_me").then(() => frm.reload_doc()));
		}
		if (manager && !merged) {
			frm.add_custom_button(__("Merge into…"), () => frm.trigger("merge_dialog"), __("Actions"));
			frm.add_custom_button(__("Split…"), () => frm.trigger("split_dialog"), __("Actions"));
		}
		if (frm.doc.postmortem) {
			frm.add_custom_button(__("Postmortem"), () => frappe.set_route("Form", "Wolf Postmortem", frm.doc.postmortem));
		} else if (manager && !merged && frm.doc.status === "Resolved") {
			frm.add_custom_button(__("Write postmortem"), () =>
				frappe
					.call({ method: `${WOLF_API}.create_postmortem`, args: { incident: frm.doc.name }, freeze: true })
					.then(({ message }) => frappe.set_route("Form", "Wolf Postmortem", message))
			);
		}
		frm.add_custom_button(__("Overview"), () => {
			frappe.route_options = {};
			frappe.set_route("wolf-overview");
		});
		if (frm.doc.nis2_significant || frm.doc.dora_major || frm.doc.gdpr_breach) {
			frm.add_custom_button(__("Regulatory report"), () => frm.trigger("regulatory_report"), __("Actions"));
			frm.trigger("render_regulatory");
		}
		frm.trigger("render_timeline");
		frm.trigger("render_deliveries");
	},

	nis2_significant(frm) {
		frm.trigger("render_regulatory");
	},

	gdpr_breach(frm) {
		frm.trigger("render_regulatory");
	},

	dora_major(frm) {
		frm.trigger("render_regulatory");
	},

	render_regulatory(frm) {
		const field = frm.get_field("regulatory_html");
		if (!field || frm.is_new()) return;
		const esc = frappe.wolf.esc;
		const STATUS = {
			open: __("open"), due_soon: __("due within 6 h"), overdue: __("OVERDUE"),
			done: __("sent on time"), done_late: __("sent late"),
		};
		frappe.call("frappe_wolf.api.compliance.get_deadlines", { incident: frm.doc.name }).then(({ message }) => {
			if (!message.length) {
				field.$wrapper.html(`<p class="text-muted">${__("Save to start the clocks.")}</p>`);
				return;
			}
			const rows = message
				.map((r) => {
					const due = r.due_at
						? frappe.datetime.str_to_user(r.due_at) + (r.estimated ? ` (${__("estimated")})` : "")
						: __("as asked");
					const sent = r.done_at ? frappe.datetime.str_to_user(r.done_at) : "—";
					const strong = r.status === "overdue" ? "font-weight:600;color:var(--red-600)" : "";
					return `<tr><td>${esc(r.label)}</td><td>${esc(due)}</td><td>${esc(sent)}</td><td style="${strong}">${esc(STATUS[r.status])}</td></tr>`;
				})
				.join("");
			field.$wrapper.html(
				`<table class="table table-sm"><thead><tr><th>${__("Obligation")}</th><th>${__("Due")}</th><th>${__("Sent")}</th><th>${__("Status")}</th></tr></thead><tbody>${rows}</tbody></table>`
			);
		});
	},

	regulatory_report(frm) {
		frappe
			.call({ method: "frappe_wolf.api.compliance.get_report", args: { incident: frm.doc.name }, freeze: true })
			.then(({ message }) => {
				const d = new frappe.ui.Dialog({
					title: __("Regulatory report draft"),
					size: "extra-large",
					fields: [
						{
							fieldtype: "HTML",
							options: `<p class="text-muted">${__("A draft to start the authority's form from. Wolf sends nothing: check every fact first.")}</p>`,
						},
						{ fieldname: "markdown", fieldtype: "Code", options: "Markdown", read_only: 1, default: message.markdown },
					],
					primary_action_label: __("Copy"),
					primary_action() {
						frappe.utils.copy_to_clipboard(message.markdown);
					},
				});
				d.show();
			});
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
				frm.__wolf_timeline = message;
				$wrap.html(
					`<div class="wolf-card">
						<div class="wolf-timeline-actions">
							<button class="btn btn-xs btn-default wolf-add-comment">${__("Add comment")}</button>
						</div>
						${frappe.wolf.render_timeline(message.events, {
							ascending: true,
							hide_incident: true,
							activity: message.activity,
						})}
					</div>`
				);
				$wrap.find(".wolf-add-comment").on("click", () => frm.trigger("comment_dialog"));
			});
	},

	comment_dialog(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Comment on {0}", [frm.doc.name]),
			fields: [{ fieldname: "content", fieldtype: "Small Text", label: __("Comment"), reqd: 1 }],
			primary_action_label: __("Add"),
			primary_action({ content }) {
				frappe
					.call({ method: `${WOLF_API}.add_comment`, args: { incident: frm.doc.name, content } })
					.then(() => {
						d.hide();
						frm.trigger("render_timeline");
					});
			},
		});
		d.show();
	},

	merge_dialog(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Merge {0} into another incident", [frm.doc.name]),
			fields: [
				{
					fieldname: "target",
					fieldtype: "Link",
					options: "Wolf Incident",
					label: __("Target incident"),
					reqd: 1,
					get_query: () => ({ filters: { name: ["!=", frm.doc.name], merged_into: ["is", "not set"] } }),
				},
				{
					fieldtype: "HTML",
					options: `<p class="text-muted small">${__(
						"Events and comments move to the target. This incident is resolved and points to it."
					)}</p>`,
				},
			],
			primary_action_label: __("Merge"),
			primary_action({ target }) {
				frappe
					.call({ method: `${WOLF_API}.merge`, args: { source: frm.doc.name, target }, freeze: true })
					.then(({ message }) => {
						d.hide();
						frappe.show_alert({ message: __("Merged into {0}", [message.target]), indicator: "green" });
						frappe.set_route("Form", "Wolf Incident", message.target);
					});
			},
		});
		d.show();
	},

	split_dialog(frm) {
		const events = ((frm.__wolf_timeline && frm.__wolf_timeline.events) || []).filter((e) => e.linked);
		if (events.length < 2) {
			frappe.msgprint(__("An incident needs at least two linked events to be split."));
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Split events into a new incident"),
			fields: [
				{ fieldname: "title", fieldtype: "Data", label: __("New incident title") },
				{
					fieldname: "events",
					fieldtype: "MultiCheck",
					label: __("Events to move"),
					reqd: 1,
					columns: 1,
					options: events.map((e) => ({
						// event titles come from agents: escape before MultiCheck renders them
						label: frappe.wolf.esc(`${(e.timestamp || "").slice(0, 16)} · ${e.title}`),
						value: e.name,
					})),
				},
			],
			primary_action_label: __("Split"),
			primary_action(values) {
				frappe
					.call({
						method: `${WOLF_API}.split`,
						args: { incident: frm.doc.name, events: values.events, title: values.title },
						freeze: true,
					})
					.then(({ message }) => {
						d.hide();
						frappe.show_alert({ message: __("Created {0}", [message]), indicator: "green" });
						frappe.set_route("Form", "Wolf Incident", message);
					});
			},
		});
		d.show();
	},
});
