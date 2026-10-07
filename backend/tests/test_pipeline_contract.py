from backend.outcome import FIELD_LABELS, extraction_outcome
from backend.pipeline import Pipeline


def test_unverified_visual_conflict_cannot_be_complete():
    proposal = {key: 'present' for key in FIELD_LABELS}
    proposal['items'] = [{'name': 'Service', 'components': [{'lineTotal': 100}]}]
    verification = {'visionRequired': True, 'visionStatus': 'vision', 'visionComplete': False}
    result = extraction_outcome(proposal, verification)
    assert result['state'] == 'partial'
    assert any(value['field'] == 'visualStructure' for value in result['unavailable'])


def test_config_changed_invalidates_pipeline_cache(monkeypatch):
    monkeypatch.setenv('SEMANTIC_MODEL_URL', 'http://model-one:8081')
    first = Pipeline._cache_key(b'document', 'offer.pdf', 'extract')
    monkeypatch.setenv('SEMANTIC_MODEL_URL', 'http://model-two:8081')
    assert first != Pipeline._cache_key(b'document', 'offer.pdf', 'extract')
    monkeypatch.setenv('SEMANTIC_MODE', 'always')
    assert first != Pipeline._cache_key(b'document', 'offer.pdf', 'extract')


def test_missing_requisites_do_not_require_repeated_inference():
    result = {'metadata': {'outcome': {'state': 'partial'}, 'verification': {
        'coverageComplete': True, 'reviewCompleted': True, 'visionRequired': False}}}
    assert Pipeline._cacheable(result)
    result['metadata']['verification'].update(visionRequired=True, visionComplete=False)
    assert not Pipeline._cacheable(result)
