from frappe.core.doctype.scheduled_job_type.scheduled_job_type import ScheduledJobType

from frappe_wolf.wolf.local_signals import on_scheduled_job_failed


class WolfScheduledJobType(ScheduledJobType):
	"""Report failed scheduled jobs to Wolf.

	Frappe only writes a Scheduled Job Log when `create_log` is set, flips its status with
	db_set (no doc events), and logs "Scheduled Job Failed" at INFO, which the default
	logger level drops. Hooking log_status is the one place every failure goes through.
	"""

	def log_status(self, status):
		super().log_status(status)
		if status == "Failed":
			on_scheduled_job_failed(self)
