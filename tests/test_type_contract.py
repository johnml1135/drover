"""The public JSON contract shared by registry, ledger, forms and importers."""
import copy
import hashlib
import unittest

from drover.tasktypes.schema import canonical_hash, normalize_type, validate_task
from drover.tasktypes import schema


class TypeContractTests(unittest.TestCase):
    def definition(self, **overrides):
        raw = {
            'schema_version': 1, 'name': 'example', 'revision': 1,
            'fields': {
                'problem': {'type': 'markdown', 'required': True},
                'enabled': {'type': 'boolean', 'default': True},
                'evidence': {'type': 'list', 'items': {'type': 'uri'},
                             'default': ['https://example.com']},
                'attempts_limit': {'type': 'integer', 'default': 3},
                'optional': {'type': 'string', 'nullable': True, 'default': 'initial'},
            },
            'acceptance': {'defaults': ['Cite evidence', 'Test behavior']},
        }
        raw.update(overrides)
        return raw

    def payload(self, **overrides):
        value = {'title': 'Example', 'repo': 'drover', 'scope_files': ['docs/'],
                 'fields': {'problem': 'Explain the problem'}}
        value.update(overrides)
        return value

    def invalid(self, path, call, *args):
        with self.assertRaises(ValueError) as caught:
            call(*args)
        self.assertIn(path, [e['path'] for e in caught.exception.errors])
        self.assertTrue(all(e['message'] for e in caught.exception.errors))

    def test_defaults_and_patch(self):
        resolved = normalize_type(self.definition())
        payload = self.payload(acceptance=['Explicit criterion', 'Cite evidence'],
                               fields={'problem': 'Why?', 'enabled': False, 'evidence': []})
        before = copy.deepcopy(payload)
        task = validate_task(payload, resolved)
        self.assertEqual(task['fields'].get('attempts_limit'), 3)
        self.assertIs(task['fields']['enabled'], False)
        self.assertEqual(task['fields']['evidence'], [])
        self.assertEqual(task['acceptance'], ['Explicit criterion', 'Cite evidence', 'Test behavior'])
        self.assertEqual(payload, before)
        patched = validate_task({'fields': {'optional': None}}, resolved, task)
        self.assertIsNone(patched['fields']['optional'])
        self.assertEqual(patched['fields']['problem'], 'Why?')
        # Old records are validated without re-materializing optional defaults.
        del patched['fields']['enabled']
        self.assertNotIn('enabled', validate_task({}, resolved, patched)['fields'])
        self.invalid('fields.problem', validate_task, {'fields': {'problem': None}}, resolved, task)
        self.invalid('fields.problem', validate_task, {'fields': {'problem': '  '}}, resolved, task)

    def test_aliases_and_unknowns(self):
        resolved = normalize_type(self.definition())
        self.invalid('type', validate_task, self.payload(type='example', kind='fix'), resolved)
        task = validate_task(self.payload(kind='example', description='A goal'), resolved)
        self.assertEqual(task['type'], task['kind'])
        self.assertEqual(task['goal'], task['description'])
        self.invalid('fields.title', normalize_type,
                     self.definition(fields={'title': {'type': 'string'}}))
        self.invalid('surprise', normalize_type, self.definition(surprise=True))
        self.invalid('surprise', validate_task, self.payload(surprise=True), resolved)
        self.invalid('fields.unlisted', validate_task,
                     self.payload(fields={'problem': 'x', 'unlisted': 1}), resolved)
        self.invalid('fields.attempts_limit', validate_task,
                     self.payload(fields={'problem': 'x', 'attempts_limit': True}), resolved)
        self.invalid('fields.attempts_limit', validate_task,
                     self.payload(fields={'problem': 'x', 'attempts_limit': float('nan')}), resolved)
        self.invalid('schema_version', normalize_type, self.definition(schema_version=True))

    def test_hash_and_placeholders(self):
        self.assertEqual(canonical_hash({'b': 2, 'a': 'é'}), canonical_hash({'a': 'é', 'b': 2}))
        self.assertEqual(canonical_hash({'a': 'é', 'b': 2}),
                         hashlib.sha256('{"a":"é","b":2}'.encode('utf-8')).hexdigest())
        self.assertNotEqual(canonical_hash({'a': 1}), canonical_hash({'a': 2}))
        self.invalid('a', canonical_hash, {'a': float('inf')})
        self.invalid('context.template', normalize_type,
                     self.definition(context={'template': '${unknown.path}'}))
        raw = self.definition(
            access={'write': True, 'read_access': ['${repo}@${default_branch}']},
            routing={'worker': {'model': '${models.director}'}},
            landing={'mode': 'spec-only', 'artifact': '${framework.design}'},
            followups={'allowed': ['grill', 'implementation', 'docs'],
                       'propose_requires': {'implementation': ['grill-complete'], 'grill': [], 'docs': []}})
        result = normalize_type(raw)
        self.assertEqual(result['followups'], raw['followups'])
        absent = normalize_type(self.definition(followups={'allowed': ['docs']}))
        self.assertEqual(absent['followups']['propose_requires'], {})
        for guards in (['grill-complete'], {'*': []}, {'fix': []}):
            with self.subTest(guards=guards):
                raw['followups']['propose_requires'] = guards
                self.invalid('followups.propose_requires' if isinstance(guards, list)
                             else 'followups.propose_requires.' + next(iter(guards)), normalize_type, raw)

    def test_placeholders_use_supported_values_not_reserved_ids(self):
        for name in ('tunable', 'forms', 'fields_json', 'schema_version', 'routing', 'approval'):
            for prefix in ('', 'task.'):
                with self.subTest(name=prefix + name):
                    self.invalid('context.template', normalize_type, self.definition(
                        context={'template': '${' + prefix + name + '}.md'}))
        for placeholder in ('${fields.missing}', '${task.fields.missing}', '${models.missing}', '${task.title'):
            with self.subTest(placeholder=placeholder):
                self.invalid('context.template', normalize_type,
                             self.definition(context={'template': placeholder + '.md'}))
        template = ('${repo}-${default_branch}-${id}-${task.id}-${title}-${task.title}-'
                    '${task.type}-${fields.problem}-${task.fields.problem}-'
                    '${framework.design}-${framework.plan}-${models.director}-${models.review}')
        self.assertEqual(normalize_type(self.definition(description=template))['description'], template)
        # Reserving a name still prevents it from becoming a custom field.
        self.invalid('fields.tunable', normalize_type,
                     self.definition(fields={'tunable': {'type': 'string'}}))

    def test_t7_access_links_and_aliases(self):
        resolved = normalize_type(self.definition(access={'read_access': ['drover@trunk']}))
        task = validate_task(self.payload(write=True, read_access=[], deps=[
            'T-a', {'depends_on': 'T-b', 'kind': 'waits-for'}], related=['T-c']), resolved)
        self.assertIs(task['write'], True)
        self.assertEqual(task['read_access'], [])
        self.assertEqual(task['deps'][1], {'depends_on': 'T-b', 'kind': 'waits-for'})
        self.assertEqual(task['related'], ['T-c'])
        self.assertEqual(validate_task({'related': []}, resolved, task)['related'], [])
        for value in ('true', 1, None):
            self.invalid('write', validate_task, self.payload(write=value), resolved)
        self.invalid('read_access.0', validate_task,
                     self.payload(read_access=['drover@../bad']), resolved)
        self.invalid('goal', validate_task, self.payload(goal='a', description='b'), resolved)
        self.invalid('deps.0.kind', validate_task,
                     self.payload(deps=[{'depends_on': 'T-b', 'kind': 'related'}]), resolved)

    def test_workspace_none_rejects_task_values_on_create_and_patch(self):
        resolved = normalize_type(self.definition(access={'write': False}))
        task = validate_task(self.payload(scope_files=[]), resolved)
        for key, value in (('branch', 'drover/T-a'), ('worktree', '/task/worktree')):
            with self.subTest(key=key):
                self.invalid(key, validate_task, self.payload(**{key: value}), resolved)
                self.invalid(key, validate_task, {key: value}, resolved, task)
                self.assertIsNone(validate_task({key: None}, resolved, task)[key])
                stored = {**task, key: value}
                self.invalid(key, validate_task, {}, resolved, stored)
        # A mode change must also reject values retained from the existing record.
        writable = normalize_type(self.definition())
        task = validate_task(self.payload(branch='drover/T-a', worktree='/task/worktree'), writable)
        no_workspace = {'write': False, 'workspace': {'branch': 'none', 'worktree': 'none'},
                        'landing': {'mode': 'no-repo-change', 'artifact': 'hub:tasks/${task.id}/result.md'}}
        self.invalid('branch', validate_task, no_workspace, writable, task)
        patched = validate_task({**no_workspace, 'branch': None, 'worktree': None}, writable, task)
        self.assertIsNone(patched['branch'])
        self.assertIsNone(patched['worktree'])
        # Explicit values remain valid when the corresponding mode permits them.
        for mode in ('new', 'existing'):
            allowed = normalize_type(self.definition(workspace={'branch': mode, 'worktree': mode}))
            self.assertEqual(validate_task(self.payload(branch='drover/T-a', worktree='/task/worktree'),
                                           allowed)['branch'], 'drover/T-a')

    def test_framework_binding_shape_on_create_and_patch(self):
        resolved = normalize_type(self.definition())
        self.invalid('framework', validate_task, self.payload(framework=42), resolved)
        binding = {
            'kind': 'superpowers', 'evidence': [{'path': 'docs/superpowers/specs/', 'exists': True}],
            'version': None, 'repo': 'drover', 'ref_sha': 'a' * 40, 'override': None,
            'root': 'docs/superpowers', 'design': 'docs/superpowers/specs/design.md',
            'plan': 'docs/superpowers/plans/plan.md', 'tasks': 'docs/superpowers/plans/tasks.json',
            'source': {'path': 'docs/superpowers/plans/tasks.json', 'metadata': {'imported': False}},
        }
        task = validate_task(self.payload(framework=binding), resolved)
        self.assertEqual(task['framework'], binding)
        self.assertEqual(validate_task({}, resolved, task)['framework'], binding)
        updated = validate_task({'framework': {'version': 'v1', 'override': 'superpowers'}}, resolved, task)
        self.assertEqual(updated['framework'], {**binding, 'version': 'v1', 'override': 'superpowers'})
        self.assertIsNone(validate_task({'framework': None}, resolved, task)['framework'])
        self.assertIsNone(validate_task(self.payload(framework=None), resolved)['framework'])
        self.assertEqual(validate_task(self.payload(framework={}), resolved)['framework'], {})
        invalid = [('framework', 42), ('framework', []), ('framework.unknown', {'unknown': True}),
                   ('framework.evidence', {'evidence': {}}),
                   ('framework.evidence.0', {'evidence': ['docs/specs/']}),
                   ('framework.source', {'source': []})]
        invalid += [('framework.' + key, {key: 42}) for key in binding if key not in ('evidence', 'source')]
        invalid += [('framework.' + key, {key: None}) for key in binding
                    if key not in ('version', 'override')]
        for path, value in invalid:
            with self.subTest(path=path, value=value):
                self.invalid(path, validate_task, self.payload(framework=value), resolved)
                self.invalid(path, validate_task, {'framework': value}, resolved, task)
                self.invalid(path, validate_task, {}, resolved, {**task, 'framework': value})
        self.assertEqual(task['framework'], binding)

    def test_field_kinds_bounds_and_required_nullable(self):
        fields = {
            'text': {'type': 'string', 'required': True, 'nullable': True},
            'markdown': {'type': 'markdown', 'min_length': 2, 'max_length': 8},
            'count': {'type': 'integer', 'minimum': 0, 'maximum': 5},
            'weight': {'type': 'number', 'minimum': 0, 'maximum': 1},
            'enabled': {'type': 'boolean'},
            'choice': {'type': 'enum', 'enum': ['a', 'b']},
            'items': {'type': 'list', 'items': {'type': 'integer'}, 'max_length': 2},
            'repository': {'type': 'repo-ref'}, 'task_ref': {'type': 'task-ref'},
            'url': {'type': 'uri'},
        }
        resolved = normalize_type(self.definition(fields=fields))
        valid = {'text': None, 'markdown': '**ok**', 'count': 0, 'weight': 0.5,
                 'enabled': False, 'choice': 'b', 'items': [], 'repository': 'drover@topic/x',
                 'task_ref': 'T-a', 'url': 'https://example.com/source'}
        self.assertEqual(validate_task(self.payload(fields=valid), resolved)['fields'], valid)
        bad_values = {'text': '', 'markdown': 'x', 'count': 6, 'weight': True,
                      'enabled': 0, 'choice': 'c', 'items': [True], 'repository': 'bad repo',
                      'task_ref': 'E-a', 'url': 'https://'}
        for name, value in bad_values.items():
            with self.subTest(name=name):
                bad = {**valid, name: value}
                self.invalid('fields.' + name + ('.0' if name == 'items' else ''),
                             validate_task, self.payload(fields=bad), resolved)
        self.invalid('fields.text', validate_task, self.payload(fields={}), resolved)
        for core_id in ('write', 'fields_json', 'type_version', 'related', 'approved_at'):
            self.invalid('fields.' + core_id, normalize_type,
                         self.definition(fields={core_id: {'type': 'string'}}))

    def test_required_enum_strings_are_nonempty_in_values_and_defaults(self):
        for blank in ('', '   ', '\t\n'):
            with self.subTest(blank=blank):
                field = {'type': 'enum', 'enum': [blank, 'valid', 0, False], 'required': True}
                resolved = normalize_type(self.definition(fields={'choice': field}))
                self.invalid('fields.choice', validate_task,
                             self.payload(fields={'choice': blank}), resolved)
                task = validate_task(self.payload(fields={'choice': 'valid'}), resolved)
                self.invalid('fields.choice', validate_task, {'fields': {'choice': blank}}, resolved, task)
                self.invalid('fields.choice.default', normalize_type,
                             self.definition(fields={'choice': {**field, 'default': blank}}))
                # Required applies the trimming rule only to string enum values.
                for value in (0, False):
                    self.assertEqual(validate_task(self.payload(fields={'choice': value}),
                                                   resolved)['fields']['choice'], value)
                optional = normalize_type(self.definition(fields={'choice': {
                    **field, 'required': False, 'default': blank}}))
                self.assertEqual(validate_task(self.payload(fields={}), optional)['fields']['choice'], blank)
        nullable = normalize_type(self.definition(fields={'choice': {
            'type': 'enum', 'enum': ['valid'], 'required': True, 'nullable': True, 'default': None}}))
        self.assertIsNone(validate_task(self.payload(fields={}), nullable)['fields']['choice'])

    def test_workspace_delivery(self):
        read_only = normalize_type(self.definition(access={'write': False, 'read_access': ['drover@trunk']}))
        task = validate_task(self.payload(write=False, scope_files=[]), read_only)
        self.assertIs(task['write'], False)
        self.assertEqual(task['scope_files'], [])
        self.invalid('workspace.worktree', validate_task, self.payload(write=True), read_only)
        writable = normalize_type(self.definition())
        self.invalid('scope_files', validate_task, self.payload(scope_files=[]), writable)
        self.invalid('repo', validate_task, self.payload(repo=None), writable)
        self.invalid('scope_files.0', validate_task, self.payload(scope_files=['../elsewhere']), writable)
        self.invalid('worktree', validate_task, self.payload(), normalize_type(
            self.definition(workspace={'worktree': 'existing', 'branch': 'new'})))
        for section, path, value in (
            ('workspace', 'workspace.worktree', {'worktree': 'none'}),
            ('review', 'review.required', {'required': False}),
            ('context', 'context.compact_at_tokens', {'compact_at_tokens': 100}),
            ('context', 'context.budget_tokens', {'budget_tokens': True}),
            ('routing', 'routing.worker.backend', {'worker': {'backend': 'shell'}}),
            ('access', 'access.sandbox', {'write': True, 'sandbox': 'read-only'}),
        ):
            with self.subTest(section=section, value=value):
                self.invalid(path, normalize_type, self.definition(**{section: value}))
        owner = normalize_type({'schema_version': 1, 'name': 'owner-answer', 'assignee': 'owner'})
        self.assertEqual(owner['routing'], {})
        self.assertIsNone(owner['access']['sandbox'])
        self.assertEqual(owner['lifecycle'], 'owner-v1')

    def test_fields_json_and_canonical_input(self):
        encoded = schema.encode_fields_json({'z': [], 'a': False, 'unicode': 'é'})
        self.assertEqual(encoded, '{"a":false,"unicode":"é","z":[]}')
        self.assertEqual(schema.decode_fields_json(encoded), {'z': [], 'a': False, 'unicode': 'é'})
        for text in ('[]', 'null', '{"x":1,"x":2}', '{"x":NaN}', '{oops'):
            with self.subTest(text=text):
                self.invalid('fields_json.x' if text == '{"x":NaN}' else 'fields_json',
                             schema.decode_fields_json, text)
        self.invalid('fields.x', schema.encode_fields_json, {'x': float('-inf')})
        self.invalid('x', canonical_hash, {'x': (1, 2)})
        self.invalid('', canonical_hash, {1: 'not a string key'})
        self.invalid('fields_json', validate_task, self.payload(fields_json='{}'),
                     normalize_type(self.definition()))

    def test_forms_tuning_and_normalization(self):
        raw = self.definition()
        normalized = normalize_type(raw)
        self.assertEqual(normalized, normalize_type(normalized))
        self.assertNotIn('forms', raw)
        self.assertIn('problem', normalized['forms']['create']['fields'])
        self.assertIn('scope_files', normalized['forms']['create']['cli']['order'])
        self.invalid('forms.create.fields', normalize_type, self.definition(forms={
            'create': {'fields': ['title', 'repo', 'goal', 'problem']}}))
        self.invalid('forms.create.cli.order', normalize_type, self.definition(forms={
            'create': {'cli': {'order': []}}}))
        self.invalid('forms.create.web.widgets.problem', normalize_type, self.definition(forms={
            'create': {'web': {'widgets': {'problem': 'raw-html'}}}}))
        self.invalid('tunable.0', normalize_type, self.definition(tunable=['access.write']))
        self.invalid('fields.problem.required', normalize_type,
                     self.definition(fields={'problem': {'type': 'string', 'required': 1}}))

    def test_invalid_shapes_have_field_errors(self):
        for raw, path in (
            (self.definition(context={'template': 3}), 'context.template'),
            (self.definition(fields={'nested': {'type': 'list', 'items': None}}), 'fields.nested.items'),
            (self.definition(landing={'artifact': 3}), 'landing.artifact'),
            (self.definition(fields=[]), 'fields'),
            (self.definition(followups={'propose_requires': None}), 'followups.propose_requires'),
        ):
            with self.subTest(path=path):
                self.invalid(path, normalize_type, raw)
        resolved = normalize_type(self.definition())
        self.invalid('access', validate_task, self.payload(access=None), resolved)

    def test_policy_patches_preserve_pinned_values_and_nulls(self):
        resolved = normalize_type(self.definition(access={'read_access': ['drover@trunk']},
            routing={'worker': {'backend': 'claude', 'model': '${models.director}'}}))
        task = validate_task(self.payload(access={'read_access': []},
            routing={'worker': {'effort': 'high'}}), resolved)
        self.assertEqual(task['routing']['worker']['backend'], 'claude')
        self.assertEqual(task['read_access'], [])
        patched = validate_task({'routing': {'worker': {'effort': 'medium'}}}, resolved, task)
        self.assertEqual(patched['routing']['worker']['model'], '${models.director}')
        self.assertEqual(patched['read_access'], [])
        self.invalid('type', validate_task, {'type': 'other'}, resolved, task)
        self.invalid('title', validate_task, {'title': None}, resolved, task)
        self.invalid('status', validate_task, {'status': 'landed'}, resolved, task)
        self.assertEqual(validate_task({'acceptance': []}, resolved, task)['acceptance'], [])
        self.invalid('access.sandbox', validate_task,
                     self.payload(access={'sandbox': 'unrestricted'}), resolved)

    def test_schema_exports_and_default_isolation(self):
        import drover.tasktypes as contracts
        for name in ('TypeSnapshot', 'Principal', 'TaskRecord', 'ActionRequest',
                     'ActionReceipt', 'FormDescriptor', 'FrameworkBinding'):
            self.assertIs(getattr(contracts, name, None), getattr(schema, name))
        resolved = normalize_type(self.definition(fields={
            'enabled': {'type': 'boolean', 'required': True, 'default': False},
            'sources': {'type': 'list', 'items': {'type': 'uri'}, 'default': []}}))
        task = validate_task(self.payload(fields={}), resolved)
        self.assertIs(task['fields']['enabled'], False)
        self.assertEqual(task['fields']['sources'], [])
        task['fields']['sources'].append('https://example.com')
        self.assertEqual(validate_task(self.payload(fields={}), resolved)['fields']['sources'], [])

    def test_worked_extension_forms_alone(self):
        raw = self.definition(
            fields={
                'problem': {'type': 'markdown', 'required': True},
                'target': {'type': 'string', 'required': True, 'min_length': 1, 'max_length': 200,
                           'shown': ['card', 'page']},
                'compatibility': {'type': 'enum', 'required': True, 'default': 'additive',
                                  'enum': ['additive', 'breaking'], 'shown': ['card', 'page']},
                'evidence': {'type': 'list', 'items': {'type': 'uri'}, 'default': [], 'shown': ['page']},
            },
            landing={'mode': 'spec-only', 'artifact': '${framework.design}', 'plan': '${framework.plan}'},
            forms={'create': {
                'fields': ['title', 'repo', 'goal', 'scope_files', 'problem', 'target', 'compatibility', 'evidence'],
                'web': {'layout': 'stacked'}, 'cli': {'mode': 'sequential', 'order': [
                    'title', 'repo', 'goal', 'scope_files', 'problem', 'target', 'compatibility', 'evidence']}},
                'answer': {'fields': [], 'web': {'layout': 'stacked'},
                           'cli': {'mode': 'sequential', 'order': []}}})
        resolved = normalize_type(raw)
        inputs = self.payload(goal='Specify orders', fields={'problem': 'Clients need an API', 'target': 'orders'})
        task = validate_task(inputs, resolved)
        self.assertEqual(task['fields']['compatibility'], 'additive')
        self.assertEqual(task['fields']['evidence'], [])
        self.invalid('fields.problem', validate_task,
                     {**inputs, 'fields': {'target': 'orders'}}, resolved)
        self.invalid('scope_files', validate_task, {**inputs, 'scope_files': []}, resolved)


if __name__ == '__main__':
    unittest.main()
