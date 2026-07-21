import unittest

from note import TEXT


class NoteTests(unittest.TestCase):
    def test_note_remains_safe(self):
        self.assertEqual(TEXT, "safe")
