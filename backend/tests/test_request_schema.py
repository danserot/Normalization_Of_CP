"""Offline checks for the provider's strict document request contract."""
import base64

from backend.model_document import DocumentAnswer, input_parts
from backend.openai_client import strict_schema


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _allows_null(node):
    kind = node.get('type')
    return (kind == 'null' or isinstance(kind, list) and 'null' in kind
            or any(_allows_null(branch) for branch in node.get('anyOf', [])))


def _nesting_depth(node, definitions):
    if '$ref' in node:
        return _nesting_depth(definitions[node['$ref'].removeprefix('#/$defs/')], definitions)
    children = list(node.get('properties', {}).values())
    children += node.get('anyOf', [])
    if isinstance(node.get('items'), dict):
        children.append(node['items'])
    return (int(node.get('type') in {'object', 'array'})
            + max((_nesting_depth(child, definitions) for child in children), default=0))


def test_default_factory_lists_cannot_become_null_in_provider_response():
    """The provider must return [] when a DTO requires a list, never null."""
    schema = strict_schema(DocumentAnswer.model_json_schema())
    arrays = [(schema, name) for name in ('cells', 'evidence', 'warnings')]
    arrays += [(schema['$defs']['ProposalData'], name)
               for name in ('items', 'additionalFields')]
    arrays += [(schema['$defs']['ProposalItem'], name)
               for name in ('components', 'additionalFields')]
    for owner, name in arrays:
        node = owner['properties'][name]
        assert name in owner['required']
        assert node['type'] == 'array'
        assert not _allows_null(node)


def test_missing_numeric_values_and_source_locations_stay_nullable():
    schema = strict_schema(DocumentAnswer.model_json_schema())
    for owner, names in (
        ('ProposalData', ('documentTotal',)),
        ('ProposalItem', ('quantity', 'unitPrice', 'lineTotal', 'componentMode')),
        ('CostComponent', ('unitPrice', 'lineTotal')),
        ('ModelCell', ('page', 'sheet')),
    ):
        for name in names:
            assert _allows_null(schema['$defs'][owner]['properties'][name])


def test_document_schema_fits_supported_strict_schema_limits_and_preserves_types():
    original = DocumentAnswer.model_json_schema()
    schema = strict_schema(original)
    objects = [node for node in _nodes(schema) if node.get('type') == 'object']
    assert sum(len(node.get('properties', {})) for node in objects) <= 5000
    assert _nesting_depth(schema, schema['$defs']) <= 10
    names_length = sum(len(name) for node in objects for name in node['properties'])
    names_length += sum(len(name) for name in schema['$defs'])
    names_length += sum(len(str(value)) for node in _nodes(schema)
                        for value in node.get('enum', []))
    assert names_length <= 120_000
    for node in objects:
        assert set(node['required']) == set(node['properties'])
        assert node['additionalProperties'] is False
    for node in _nodes(schema):
        assert not {'allOf', 'oneOf', 'not', 'if', 'then', 'else',
                    'dependentRequired', 'dependentSchemas'} & node.keys()
        assert 'default' not in node
        reference = node.get('$ref')
        if reference:
            assert reference.startswith('#/$defs/')
            assert reference.removeprefix('#/$defs/') in schema['$defs']

    # Normalization must not weaken declared DTO types or numeric/text limits.
    for name, source in original['$defs'].items():
        target = schema['$defs'][name]
        assert set(target['properties']) == set(source['properties'])
        for field, node in source['properties'].items():
            cleaned = {key: value for key, value in node.items()
                       if key not in {'default', 'examples'}}
            assert target['properties'][field] == cleaned


def test_tsv_original_file_uses_documented_input_file_mime():
    content = b'name\tquantity\tprice\nItem\t2\t10\n'
    part, = input_parts(content, 'offer.tsv')
    assert part['type'] == 'input_file'
    assert part['filename'] == 'offer.tsv'
    assert part['file_data'].startswith('data:text/tsv;base64,')
    assert base64.b64decode(part['file_data'].split(',', 1)[1]) == content
