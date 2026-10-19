"""Hub-only YAML decoding; worker and client contracts use normalized JSON."""

import math
import re

from .schema import ValidationError


def _fail(message, mark=None):
    if mark is not None:
        message = f'line {mark.line + 1}, column {mark.column + 1}: {message}'
    raise ValidationError('', message)


def decode_yaml(text: str) -> dict:
    """Decode a task type or lifecycle mapping without activating a registry."""
    if not isinstance(text, str):
        _fail('YAML input must be UTF-8 text')
    try:
        size = len(text.encode('utf-8'))
    except UnicodeEncodeError:
        _fail('YAML input must be valid UTF-8 text')
    if size > 256 * 1024:
        _fail('YAML input exceeds 256 KiB of UTF-8 text')

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            'YAML decoding requires the owner-approved PyYAML==6.0.3 in the hub '
            'environment. Run validation in the configured hub environment or '
            'ask the owner to provision that dependency; workers and CLI use normalized JSON.'
        ) from exc

    class BoundedSafeLoader(yaml.SafeLoader):
        yaml_implicit_resolvers = {}
        # Root is depth zero, matching the format-independent JSON contract.
        depth = -1

        def compose_node(self, parent, index):
            event = self.peek_event()
            if isinstance(event, yaml.AliasEvent):
                _fail('YAML aliases are forbidden', event.start_mark)
            if event.anchor is not None:
                _fail('YAML anchors are forbidden', event.start_mark)
            if event.tag is not None:
                _fail('YAML tags are forbidden', event.start_mark)
            if self.depth >= 32:
                _fail('YAML nesting exceeds 32', event.start_mark)
            self.depth += 1
            try:
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1

        def construct_scalar(self, node):
            value = super().construct_scalar(node)
            if node.style is None and re.fullmatch(r'[+-]?\.?(?:inf(?:inity)?|nan)', value, re.I):
                _fail('YAML numbers must be finite', node.start_mark)
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                _fail('YAML strings must be valid UTF-8 text', node.start_mark)
            return value

        def construct_mapping(self, node, deep=False):
            mapping = {}
            for key_node, value_node in node.value:
                if key_node.value == '<<':
                    _fail('YAML merge keys are forbidden', key_node.start_mark)
                key = self.construct_object(key_node, deep=True)
                if not isinstance(key, str):
                    _fail('YAML mapping keys must be strings', key_node.start_mark)
                if key in mapping:
                    _fail(f'duplicate YAML key {key!r}', key_node.start_mark)
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    for kind, pattern, first in (
        ('bool', r'(?:true|false)', 'tf'),
        ('null', r'null', 'n'),
        ('int', r'-?(?:0|[1-9][0-9]*)', '-0123456789'),
        ('float', r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)',
         '-0123456789'),
    ):
        BoundedSafeLoader.add_implicit_resolver(
            'tag:yaml.org,2002:' + kind, re.compile(pattern + r'\Z'), list(first))

    def finite_float(loader, node):
        value = loader.construct_yaml_float(node)
        if not math.isfinite(value):
            _fail('YAML numbers must be finite', node.start_mark)
        return value

    BoundedSafeLoader.add_constructor('tag:yaml.org,2002:float', finite_float)

    # Use only SafeLoader constructors, with restrictions applied before node
    # construction. Do not mutate PyYAML's shared loader/resolver tables.
    loader = None
    try:
        loader = BoundedSafeLoader(text)
        value = loader.get_single_data()
    except yaml.YAMLError as exc:
        _fail(f'invalid YAML: {exc}')
    finally:
        if loader is not None:
            loader.dispose()
    if not isinstance(value, dict):
        _fail('YAML must contain one mapping document')

    definitions = [value.get('fields'), value.get('states')]
    if isinstance(value.get('lifecycle'), dict):
        definitions.append(value['lifecycle'].get('states'))
    if sum(len(item) for item in definitions if isinstance(item, (dict, list))) > 1000:
        _fail('YAML permits at most 1000 combined field/state definitions')
    return value
