import json
import shutil
from collections import Counter

import pytest

from app.config import ROOT
from app.evaluation import aggregate, grouped_metrics, load_benchmark, score_case


def case(case_id='A', category='multi_evidence', split='dev'):
    return {'id': case_id, 'split': split, 'category': category, 'question': '测试问题',
            'evidence': [] if category == 'unanswerable' else [
                {'document': 'a.md', 'contains': '审批'}, {'document': 'b.md', 'contains': '金额'}]}


def hit(document, text):
    return {'document_name': document, 'text': text, 'ordinal': 1, 'score': 0.5}


def test_partial_multi_evidence_and_wrong_document_do_not_count_as_full_hit():
    hits = [hit('wrong.md', '审批与金额'), hit('a.md', '需要审批'), hit('b.md', '金额5000')]
    at_one = score_case(case(), hits, 1)
    assert at_one['evidence_recall'] == 0
    assert at_one['reciprocal_rank'] == 0
    partial = score_case(case(), hits, 2)
    assert partial['full_hit'] is False
    assert partial['evidence_recall'] == 0.5
    assert partial['reciprocal_rank'] == 0.5
    full = score_case(case(), hits, 3)
    assert full['full_hit'] is True
    assert full['evidence_recall'] == 1
    # Two evidence units in one document may be satisfied by the same chunk.
    same_document = case()
    same_document['evidence'][1]['document'] = 'a.md'
    assert score_case(same_document, [hit('a.md', '审批金额')], 1)['full_hit'] is True


def test_no_answer_denominators_and_empty_groups():
    positive = score_case(case(), [hit('a.md', '审批')], 1)
    negative = score_case(case('N1', 'unanswerable', 'test'), [hit('a.md', '审批')], 1)
    rejected = score_case(case('N2', 'unanswerable', 'test'), [], 1)
    metrics = aggregate([positive, negative, rejected])
    assert metrics['answerable_count'] == 1
    assert metrics['unanswerable_count'] == 2
    assert metrics['full_evidence_hit_rate'] == 0
    assert metrics['mean_evidence_recall'] == 0.5
    assert metrics['mrr'] == 1
    assert metrics['unanswerable_retrieval_rate'] == 0.5
    assert negative['full_hit'] is None and negative['reciprocal_rank'] is None
    assert aggregate([])['mrr'] is None
    grouped = grouped_metrics([positive, negative, rejected])
    assert grouped['test']['mrr'] is None
    assert grouped['dev']['unanswerable_retrieval_rate'] is None
    assert grouped['test']['by_category']['unanswerable']['case_count'] == 2


def test_versioned_dataset_evidence_is_present_in_real_chunks():
    cases, documents, metadata = load_benchmark(ROOT / 'evals/benchmark.json', ROOT / 'evals/corpus')
    assert len(cases) == 60 and len(documents) == 16
    assert Counter(c['split'] for c in cases) == {'dev': 26, 'test': 34}
    assert Counter(c['category'] for c in cases) == {
        'literal': 16, 'paraphrase': 16, 'scope': 8, 'multi_evidence': 8, 'unanswerable': 12}
    assert metadata['chunk_count'] >= 16
    assert len(metadata['corpus_sha256']) == 64


def test_public_corpus_provenance_and_evidence(tmp_path):
    original = ROOT / 'evals/public-fastapi'
    target = tmp_path / 'public'
    shutil.copytree(original, target)
    cases, documents, metadata = load_benchmark(target / 'benchmark.json', target / 'corpus')
    assert len(cases) == 30 and len(documents) == 6
    assert metadata['chunk_count'] > len(documents)
    assert Counter(c['split'] for c in cases) == {'dev': 10, 'test': 20}
    assert Counter(c['category'] for c in cases) == {
        'literal': 6, 'paraphrase': 6, 'scope': 6, 'multi_evidence': 6, 'unanswerable': 6}
    assert metadata['provenance']['license'] == 'MIT'
    assert metadata['provenance']['revision'] == '0.115.0'
    # A changed source must be rejected even when the labeled quote still exists.
    body = target / 'corpus/body.md'
    body.write_bytes(body.read_bytes() + b'\nchanged')
    with pytest.raises(ValueError, match='SHA-256'):
        load_benchmark(target / 'benchmark.json', target / 'corpus')
    shutil.copyfile(original / 'corpus/body.md', body)
    (target / 'LICENSE').unlink()
    with pytest.raises(ValueError, match='LICENSE'):
        load_benchmark(target / 'benchmark.json', target / 'corpus')


@pytest.mark.parametrize('invalid', ['duplicate_id', 'missing_quote', 'wrong_document', 'negative_evidence'])
def test_invalid_labels_are_rejected(tmp_path, invalid):
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    (corpus / 'a.md').write_text('审批与金额', encoding='utf-8')
    row = case()
    row['evidence'][1]['document'] = 'a.md'
    rows = [row]
    if invalid == 'duplicate_id':
        rows.append(row.copy())
    elif invalid == 'missing_quote':
        row['evidence'][0]['contains'] = '不存在的证据'
    elif invalid == 'wrong_document':
        row['evidence'][0]['document'] = '../a.md'
    else:
        row['category'] = 'unanswerable'
    dataset = tmp_path / 'dataset.json'
    dataset.write_text(json.dumps({'version': 'test', 'cases': rows}), encoding='utf-8')
    with pytest.raises(ValueError):
        load_benchmark(dataset, corpus)
