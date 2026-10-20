"""Exercise the shipped refresh script without a browser or third-party packages."""
from pathlib import Path
import subprocess
import unittest


def javascript(source):
    subprocess.run(['node', '-e', source], check=True, capture_output=True, text=True)


class RefreshTests(unittest.TestCase):
    def test_prefilled_edit_form_refreshes_until_values_are_changed(self):
        app = (Path(__file__).resolve().parents[1] / 'hub/static/app.js').read_text()
        setup = r'''
var requests = 0, interval, events = {}, controls = [];
function element() {
  return {hidden: true, open: false, addEventListener: function () {},
          querySelector: function () { return null; },
          querySelectorAll: function () { return []; }};
}
var content = element(), pill = element(), sheet = element();
content.querySelectorAll = function (selector) {
  return selector.indexOf('textarea') >= 0 ? controls : [];
};
var elements = {'#content': content, '#new-updates': pill, '#sheet': sheet,
  '#toast': element(), '#refresh': element(), '#sheet-close': element()};
var document = {body: element(),
  querySelector: function (selector) { return elements[selector] || null; },
  querySelectorAll: function () { return []; },
  addEventListener: function (name, fn) { events[name] = fn; }};
var window = {addEventListener: function () {}}, location = {pathname: '/tasks/T-7', search: ''};
var htmx = {ajax: function () { requests++; }};
function setInterval(fn) { interval = fn; }
function assert(ok, message) { if (!ok) throw new Error(message); }
var title = {tagName: 'INPUT', type: 'text', value: 'Prefilled title', defaultValue: 'Prefilled title'};
var description = {tagName: 'TEXTAREA', value: 'Prefilled description', defaultValue: 'Prefilled description'};
var select = {tagName: 'SELECT', type: 'select-one', selectedIndex: 0,
              options: [{selected: true, defaultSelected: false}, {selected: false, defaultSelected: false}]};
var checkbox = {tagName: 'INPUT', type: 'checkbox', checked: true, defaultChecked: true};
controls = [title, description, select, checkbox];
'''
        checks = r'''
interval();
assert(requests === 1, 'Untouched prefilled task must refresh');
title.value = 'Edited title'; interval();
assert(requests === 1 && !pill.hidden, 'Title edit must pause refresh');
title.value = title.defaultValue; interval();
assert(requests === 2, 'Reverting title must allow refresh');
description.value = 'Draft'; events['htmx:sseMessage']();
assert(requests === 2, 'Description draft must pause SSE refresh');
description.value = description.defaultValue;
select.selectedIndex = 1; select.options[0].selected = false; select.options[1].selected = true;
interval(); assert(requests === 2, 'Changed select must pause refresh');
select.selectedIndex = 0; select.options[0].selected = true; select.options[1].selected = false;
checkbox.checked = false; interval();
assert(requests === 2, 'Changed checkbox must pause refresh');
checkbox.checked = true; interval();
assert(requests === 3, 'Restored form must refresh');
'''
        javascript(setup + app + checks)
