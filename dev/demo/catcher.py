"""Tiny webhook sink: prints every alert Wolf posts, so the demo can show notifications."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
		try:
			text = json.loads(body).get("text", body.decode())
		except ValueError:
			text = body.decode(errors="replace")
		print("=== WEBHOOK ===\n" + text, flush=True)
		with open("/tmp/webhooks.log", "a") as fh:
			fh.write(body.decode(errors="replace") + "\n")
		self.send_response(200)
		self.end_headers()

	def log_message(self, *args):
		pass


HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
