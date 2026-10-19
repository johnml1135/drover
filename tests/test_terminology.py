"""Owner-facing text uses the GitHub/CI vocabulary; retired words stay out of visible UI text."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETIRED = re.compile(r"\b(gate|gated|gates|landing|landings|landed|garden|docket|rung|receipt|batch)\b", re.I)
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
TAG = re.compile(r"<[^>]*>")


def visible(text):
    return TAG.sub(" ", JINJA.sub(" ", text))


class TerminologyTest(unittest.TestCase):
    def test_templates_show_no_retired_words(self):
        for p in sorted((ROOT / "hub" / "templates").glob("*.html")):
            found = RETIRED.findall(visible(p.read_text(encoding="utf-8")))
            self.assertEqual(found, [], f"{p.name} shows retired terms")

    def test_tab_labels(self):
        text = (ROOT / "hub" / "web.py").read_text(encoding="utf-8")
        self.assertIn('("landing", "Merge queue")', text)

    def test_glossary_defines_new_terms_and_retired_table(self):
        g = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        for term in ("Checks", "Full CI", "CI result", "CI runner", "Merge queue", "Merge group", "Merged",
                     "Inbox", "Escalation level", "Waiting on you", "Cleanup", "Retired terms"):
            self.assertIn(term, g)
        self.assertIn("| receipt", g)


if __name__ == "__main__":
    unittest.main()
