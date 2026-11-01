"""Visible pane copy never uses the words the glossary retired."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted({*ROOT.glob('drover/pane*.py'), ROOT / 'drover/handoff.py', ROOT / 'drover/panes.py'})
TEMPLATES = sorted((ROOT / 'hub/templates').glob('*.html'))
CASED = re.compile(r'\b(?:READY|LAND|HOLD)\b')
LOWER = re.compile(r'(?<![A-Za-z0-9_])(?:lane|receipt|garden|gate)(?![A-Za-z0-9_])', re.IGNORECASE)


def strings(path):
    """Every string literal of a module that is not a docstring, with its line."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs]


def retired(text):
    return LOWER.findall(text) + CASED.findall(text)


class VocabularyTest(unittest.TestCase):
    def test_retired_words_are_absent_from_pane_copy(self):
        found = []
        for path in FILES:
            for line, text in strings(path):
                found += [f'{path.relative_to(ROOT)}:{line}: {word}' for word in retired(text)]
        for path in TEMPLATES:
            text = re.sub(r'<(script|style)\b.*?</\1>|\{#.*?#\}|<!--.*?-->', '', path.read_text(encoding='utf-8'), flags=re.S)
            found += [f'{path.relative_to(ROOT)}: {word}' for word in retired(re.sub(r'<[^>]*>|\{[{%].*?[}%]\}', ' ', text, flags=re.S))]
        self.assertEqual([], found)

    def test_the_scan_finds_a_retired_word(self):
        self.assertEqual(['lane', 'HOLD'], retired('a lane is on HOLD'))
        self.assertEqual([], retired('the task is ready; hold; Ready'))


if __name__ == '__main__':
    unittest.main()
