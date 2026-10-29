"""Agent secrets: generated here, shown once, stored only as a salted hash.

Format: `hmac-sha256$<salt hex>$<digest hex>`, digest = HMAC-SHA256(key=salt, msg=secret).

A fast salted hash is enough here, unlike for passwords: the secret is 160 random bits
(`secrets.token_hex(20)`), so an attacker holding the hash still has to search 2**160
values, and a slow KDF (PBKDF2, bcrypt) would only add CPU time to every push, which is
authenticated twice a minute per agent. The per-agent salt keeps equal secrets from
having equal hashes and rules out precomputed tables. The scheme prefix leaves room
for another algorithm later.
"""

import hashlib
import hmac
import secrets

SCHEME = "hmac-sha256"
SECRET_BYTES = 20  # 40 hex characters
_DUMMY = f"{SCHEME}${'0' * 32}${'0' * 64}"


def new_secret():
	return secrets.token_hex(SECRET_BYTES)


def _digest(salt, secret):
	return hmac.new(salt.encode(), secret.encode(), hashlib.sha256).hexdigest()


def hash_secret(secret, salt=None):
	if not secret:
		raise ValueError("empty secret")
	salt = salt or secrets.token_hex(16)
	return f"{SCHEME}${salt}${_digest(salt, secret)}"


def verify(secret, stored):
	"""Constant-time check of `secret` against a stored hash. A missing or malformed hash
	still costs one digest, so unknown agent ids answer as fast as wrong secrets."""
	parts = (stored or "").split("$")
	valid = len(parts) == 3 and parts[0] == SCHEME and bool(parts[1]) and bool(parts[2])
	_, salt, expected = parts if valid else _DUMMY.split("$")
	ok = hmac.compare_digest(_digest(salt, str(secret or "")), expected)
	return bool(ok and valid and secret)
