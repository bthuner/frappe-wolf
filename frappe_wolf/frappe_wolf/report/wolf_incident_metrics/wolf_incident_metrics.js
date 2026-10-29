// Copyright (c) 2026, Frappe Wolf contributors
// For license information, please see license.txt

frappe.query_reports["Wolf Incident Metrics"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: [
				{ value: "site", label: __("Site") },
				{ value: "bench", label: __("Bench") },
				{ value: "host", label: __("Host") },
				{ value: "alert_rule", label: __("Alert Rule") },
				{ value: "severity", label: __("Severity") },
			],
			default: "site",
			reqd: 1,
		},
		{ fieldname: "site", label: __("Site"), fieldtype: "Data" },
		{ fieldname: "bench", label: __("Bench"), fieldtype: "Data" },
		{ fieldname: "alert_rule", label: __("Alert Rule"), fieldtype: "Link", options: "Wolf Alert Rule" },
		{ fieldname: "severity", label: __("Severity"), fieldtype: "Select", options: ["", "info", "warning", "critical"] },
	],
	formatter(value, row, column, data, default_formatter) {
		const out = default_formatter(value, row, column, data);
		return data && data.bold ? `<strong>${out}</strong>` : out;
	},
};
