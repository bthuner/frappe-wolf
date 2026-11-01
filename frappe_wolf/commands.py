"""bench commands of Frappe Wolf."""

import click
from frappe.commands import get_site, pass_context


@click.command("wolf-verify-journal")
@click.option("--anchor", help="seq:mac copied earlier (Wolf → Security, or the SIEM): check it is still in the chain")
@pass_context
def verify_journal(context, anchor=None):
	"""Check the Wolf journal's HMAC chain: every digest, every mac, no gap."""
	import frappe

	from frappe_wolf.api.journal import verify_journal as verify

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = verify(anchor)
	finally:
		frappe.destroy()
	if result["ok"]:
		click.echo(f"journal OK: {result['checked']} entries, seq {result.get('first_seq')} to {result.get('last_seq')}, "
		           f"{result.get('pending', 0)} waiting to be signed; anchor {result.get('last_seq')}:{result.get('last_mac')}")
	else:
		problem = result["problem"] or {}
		click.echo(f"journal BROKEN at seq {problem.get('seq')}: {problem.get('why')} ({result['checked']} entries checked)", err=True)
		raise SystemExit(1)


commands = [verify_journal]
