"""Rotation-aware log tailing with persisted offsets, plus a journalctl follower."""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)
MAX_READ_BYTES = 10 * 1024 * 1024  # per file per poll


class FileTailer:
	"""Follows one file across rotation (inode change) and truncation.

	`state` is a shared dict `{path: {"inode": int, "offset": int}}` persisted by
	`TailState`. A path never seen before starts at EOF.
	"""

	def __init__(self, path, state, from_start=False):
		self.path = path
		self.state = state
		self.from_start = from_start
		self.fh = None
		self.inode = None
		self.buffer = b""

	def _open(self, offset):
		self.close()
		self.fh = open(self.path, "rb")
		self.inode = os.fstat(self.fh.fileno()).st_ino
		self.fh.seek(offset)
		self.buffer = b""

	def _open_initial(self):
		try:
			st = os.stat(self.path)
		except OSError:
			return False
		saved = self.state.get(self.path)
		if saved and saved.get("inode") == st.st_ino and saved.get("offset", 0) <= st.st_size:
			offset = saved["offset"]
		elif saved:
			offset = 0  # rotated or truncated while we were not running
		else:
			offset = 0 if self.from_start else st.st_size
		self._open(offset)
		return True

	def _read_available(self):
		chunk = self.fh.read(MAX_READ_BYTES)
		if not chunk:
			return []
		data = self.buffer + chunk
		lines = data.split(b"\n")
		self.buffer = lines.pop()  # incomplete trailing line
		return [raw.decode("utf-8", errors="replace") for raw in lines]

	def read_lines(self):
		try:
			if self.fh is None and not self._open_initial():
				return []
			lines = self._read_available()
			try:
				st = os.stat(self.path)
			except FileNotFoundError:
				return lines  # rotated away, new file not created yet: keep the old handle
			if st.st_ino != self.inode:
				# rotated: the old handle is drained above, continue with the new file
				lines += self._finish_buffer()
				self._open(0)
				lines += self._read_available()
			elif st.st_size < self.fh.tell():
				log.info("%s was truncated, reading from start", self.path)
				self._open(0)
				lines += self._read_available()
		except OSError as e:
			log.warning("cannot read %s: %s", self.path, e)
			self.close()
			return []
		self.state[self.path] = {"inode": self.inode, "offset": self.fh.tell() - len(self.buffer)}
		return lines

	def _finish_buffer(self):
		if self.buffer:
			line = self.buffer.decode("utf-8", errors="replace")
			self.buffer = b""
			return [line]
		return []

	def close(self):
		if self.fh:
			self.fh.close()
			self.fh = None


class JournalTailer:
	"""Streams `journalctl -f` output through a background reader thread."""

	def __init__(self, units=("ssh", "sshd")):
		self.cmd = ["journalctl", "-f", "-n", "0", "-o", "cat"]
		for unit in units:
			self.cmd += ["-u", unit]
		self.path = "journal:" + ",".join(units)
		self.queue = queue.Queue(maxsize=100000)
		self.proc = None

	@staticmethod
	def available():
		return shutil.which("journalctl") is not None

	def _start(self):
		self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
		threading.Thread(target=self._pump, daemon=True).start()

	def _pump(self):
		for line in self.proc.stdout:
			try:
				self.queue.put_nowait(line.rstrip("\n"))
			except queue.Full:
				pass

	def read_lines(self):
		if self.proc is None or self.proc.poll() is not None:
			self._start()
		lines = []
		while True:
			try:
				lines.append(self.queue.get_nowait())
			except queue.Empty:
				return lines

	def close(self):
		if self.proc and self.proc.poll() is None:
			self.proc.terminate()


class TailState:
	"""JSON file holding tail offsets so restarts neither skip nor replay lines."""

	def __init__(self, path):
		self.path = path
		self.data = {}
		if not path:
			return
		try:
			with open(path) as f:
				self.data = json.load(f)
		except (OSError, ValueError):
			self.data = {}

	def save(self):
		if not self.path:
			return
		tmp = self.path + ".tmp"
		try:
			os.makedirs(os.path.dirname(self.path), exist_ok=True)
			with open(tmp, "w") as f:
				json.dump(self.data, f)
			os.replace(tmp, self.path)
		except OSError as e:
			log.warning("cannot save tail state %s: %s", self.path, e)
