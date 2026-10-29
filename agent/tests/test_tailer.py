import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.tailer import FileTailer, TailState  # noqa: E402


class TestFileTailer(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.tmp.name, "app.log")

	def tearDown(self):
		self.tmp.cleanup()

	def write(self, text, mode="a", path=None):
		with open(path or self.path, mode) as f:
			f.write(text)

	def test_starts_at_eof_on_first_run(self):
		self.write("old line\n")
		t = FileTailer(self.path, {})
		self.assertEqual(t.read_lines(), [])
		self.write("new line\n")
		self.assertEqual(t.read_lines(), ["new line"])

	def test_from_start(self):
		self.write("old line\n")
		self.assertEqual(FileTailer(self.path, {}, from_start=True).read_lines(), ["old line"])

	def test_missing_file_then_created(self):
		t = FileTailer(self.path, {})
		self.assertEqual(t.read_lines(), [])
		self.write("")
		self.assertEqual(t.read_lines(), [])
		self.write("hello\n")
		self.assertEqual(t.read_lines(), ["hello"])

	def test_partial_line_buffered(self):
		self.write("")
		t = FileTailer(self.path, {})
		t.read_lines()
		self.write("par")
		self.assertEqual(t.read_lines(), [])
		self.write("tial\nnext")
		self.assertEqual(t.read_lines(), ["partial"])
		self.write("\n")
		self.assertEqual(t.read_lines(), ["next"])

	def test_rotation_by_rename(self):
		self.write("")
		t = FileTailer(self.path, {})
		t.read_lines()
		self.write("before rotate\n")
		os.rename(self.path, self.path + ".1")
		self.write("tail of old\n", path=self.path + ".1")
		self.assertEqual(t.read_lines(), ["before rotate", "tail of old"])  # new file not created yet
		self.write("after rotate\n")
		self.assertEqual(t.read_lines(), ["after rotate"])
		self.write("more\n")
		self.assertEqual(t.read_lines(), ["more"])

	def test_rotation_with_unread_data_in_both(self):
		self.write("")
		t = FileTailer(self.path, {})
		t.read_lines()
		self.write("last old\n")
		os.rename(self.path, self.path + ".1")
		self.write("first new\n")
		self.assertEqual(t.read_lines(), ["last old", "first new"])

	def test_truncation(self):
		self.write("")
		t = FileTailer(self.path, {})
		t.read_lines()
		self.write("a long line that makes the file big\n")
		t.read_lines()
		self.write("short\n", mode="w")  # copytruncate style
		self.assertEqual(t.read_lines(), ["short"])

	def test_offsets_persist_across_restart(self):
		state_path = os.path.join(self.tmp.name, "state", "tail.json")
		self.write("")
		state = TailState(state_path)
		t = FileTailer(self.path, state.data)
		t.read_lines()
		self.write("one\n")
		self.assertEqual(t.read_lines(), ["one"])
		state.save()
		t.close()

		self.write("two\n")  # written while the agent was down
		state = TailState(state_path)
		t = FileTailer(self.path, state.data)
		self.assertEqual(t.read_lines(), ["two"])
		t.close()

	def test_rotated_while_down_reads_new_file_from_start(self):
		state_path = os.path.join(self.tmp.name, "tail.json")
		self.write("x\n")
		state = TailState(state_path)
		t = FileTailer(self.path, state.data)
		t.read_lines()
		state.save()
		t.close()
		os.rename(self.path, self.path + ".1")
		self.write("fresh\n")
		t = FileTailer(self.path, TailState(state_path).data)
		self.assertEqual(t.read_lines(), ["fresh"])
		t.close()

	def test_in_memory_state(self):
		state = TailState(None)
		self.assertEqual(state.data, {})
		state.save()  # no-op


if __name__ == "__main__":
	unittest.main()
