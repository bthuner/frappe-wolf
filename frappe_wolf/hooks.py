app_name = "frappe_wolf"
app_title = "Frappe Wolf"
app_publisher = "Frappe Wolf contributors"
app_description = "Observability for Frappe benches, ERPNext/Dokos sites and Linux hosts"
app_email = "theohunerblaes@me.com"
app_license = "MIT"

required_apps = ["frappe"]

app_include_js = "/assets/frappe_wolf/js/wolf.js"
app_include_css = "/assets/frappe_wolf/css/wolf.css"

add_to_apps_screen = [
	{
		"name": "frappe_wolf",
		"logo": "/assets/frappe_wolf/images/wolf.png",
		"title": "Wolf",
		"route": "/app/wolf-overview",
	}
]

after_install = "frappe_wolf.install.after_install"
after_migrate = "frappe_wolf.install.after_migrate"

override_doctype_class = {
	"Scheduled Job Type": "frappe_wolf.overrides.scheduled_job_type.WolfScheduledJobType",
}

doc_events = {
	"Error Log": {"after_insert": "frappe_wolf.wolf.local_signals.on_error_log"},
	"Activity Log": {"after_insert": "frappe_wolf.wolf.local_signals.on_activity_log"},
	"User": {"on_update": "frappe_wolf.wolf.local_signals.on_user_update"},
}

scheduler_events = {
	"cron": {
		"* * * * *": [
			"frappe_wolf.tasks.evaluate_rules",
			"frappe_wolf.tasks.check_incidents",
			"frappe_wolf.tasks.forward_outbox",
		],
		"0 * * * *": ["frappe_wolf.tasks.send_digests"],
		"*/5 * * * *": ["frappe_wolf.tasks.check_agents"],
		"17 3 * * *": ["frappe_wolf.tasks.purge_old_data"],
	},
}
