import unittest

from note import TEXT


class NoteTests(unittest.TestCase):
    def test_note_after_edit(self):
        self.assertEqual(TEXT, "after")
