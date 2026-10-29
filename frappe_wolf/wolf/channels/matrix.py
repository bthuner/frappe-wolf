"""Matrix room message through the client-server API (pure).

PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}
https://spec.matrix.org/v1.11/client-server-api/#put_matrixclientv3roomsroomidsendeventtypetxnid
HTML body (org.matrix.custom.html):
https://spec.matrix.org/v1.11/client-server-api/#mroommessage-msgtypes
"""

from urllib.parse import quote

from frappe_wolf.wolf.channels import join_url


def build_payload(msg, cfg):
	room = (cfg.get("matrix_room_id") or "").strip()
	# txnId makes the PUT idempotent: a retry of the same attempt is not posted twice.
	txn = f"wolf-{msg.get('attempt_id') or msg.get('name') or 'msg'}"
	path = f"_matrix/client/v3/rooms/{quote(room, safe='')}/send/m.room.message/{quote(txn, safe='')}"
	return [
		{
			"method": "PUT",
			"url": join_url(cfg["matrix_homeserver"], path),
			"headers": {"Authorization": f"Bearer {cfg['matrix_access_token']}"},
			"json": {
				"msgtype": "m.text",
				"body": msg["text"],
				"format": "org.matrix.custom.html",
				"formatted_body": f"<p><strong>{msg['subject_html']}</strong></p><p>{msg['html']}</p>",
			},
			"action": "send",
		}
	]
