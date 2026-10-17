"""Evidence-level retrieval evaluation; never calls a generation API."""
import hashlib
import json
import statistics
from pathlib import Path

from app.documents import parse_document


def verify_source_manifest(manifest_path: Path, corpus_dir: Path):
    """Bind vendored public documents and their license to the recorded bytes."""
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not manifest.get('repository') or not manifest.get('revision') or not manifest.get('license'):
        raise ValueError('公开资料必须记录仓库、版本和许可。')
    names = set()
    for entry in manifest.get('files', []):
        name = entry['file']
        if not name or Path(name).name != name or name in names:
            raise ValueError(f'无效或重复的来源文件名：{name}')
        names.add(name)
        path = (corpus_dir if name.endswith('.md') else manifest_path.parent) / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError(f'公开资料缺失或 SHA-256 不符：{name}')
    actual = {path.name for path in corpus_dir.glob('*.md')}
    if {name for name in names if name.endswith('.md')} != actual or not actual or 'LICENSE' not in names:
        raise ValueError('来源清单必须覆盖全部 Markdown 原文及 LICENSE。')
    return {**manifest, 'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def load_benchmark(dataset_path: Path, corpus_dir: Path):
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    provenance = None
    if source_manifest := dataset.get('source_manifest'):
        if Path(source_manifest).name != source_manifest:
            raise ValueError('source_manifest 必须是题集同目录的文件名。')
        provenance = verify_source_manifest(dataset_path.parent / source_manifest, corpus_dir)
    documents = {path.name: path.read_bytes() for path in sorted(corpus_dir.glob('*.md'))}
    if not documents or not dataset.get('cases') or not dataset.get('version'):
        raise ValueError('评测版本、问题和资料不能为空。')
    parsed = {name: parse_document(name, body) for name, body in documents.items()}
    ids = set()
    for case in dataset['cases']:
        case_id = case.get('id')
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError(f'无效或重复的问题 ID：{case_id}')
        ids.add(case_id)
        if case.get('split') not in {'dev', 'test'}:
            raise ValueError(f'{case_id}: split 必须为 dev 或 test。')
        if case.get('category') not in {'literal', 'paraphrase', 'scope', 'multi_evidence', 'unanswerable'}:
            raise ValueError(f'{case_id}: 无效的问题类别。')
        if not isinstance(case.get('question'), str) or not case['question'].strip():
            raise ValueError(f'{case_id}: 问题不能为空。')
        evidence = case.get('evidence')
        if not isinstance(evidence, list) or (bool(evidence) == (case['category'] == 'unanswerable')):
            raise ValueError(f'{case_id}: 无答案问题必须无证据，其余问题必须提供证据。')
        if not evidence and not case.get('reason'):
            raise ValueError(f'{case_id}: 无答案问题须说明原因。')
        seen = set()
        for unit in evidence:
            name, quote = unit.get('document'), unit.get('contains')
            if name not in parsed or not isinstance(quote, str) or not quote.strip():
                raise ValueError(f'{case_id}: 文件或证据标注无效。')
            if (name, quote) in seen:
                raise ValueError(f'{case_id}: 重复证据。')
            seen.add((name, quote))
            if not any(quote in chunk.text for chunk in parsed[name]):
                raise ValueError(f'{case_id}: 证据不在解析后的片段中：{name}: {quote}')
    digest = hashlib.sha256()
    for name, body in documents.items():
        digest.update(name.encode('utf-8') + b'\0' + hashlib.sha256(body).digest())
    metadata = {
        'version': dataset['version'], 'description': dataset.get('description', ''),
        'dataset_sha256': hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        'corpus_sha256': digest.hexdigest(), 'document_count': len(documents),
        'chunk_count': sum(len(chunks) for chunks in parsed.values()),
        'total_case_count': len(dataset['cases']),
        'documents': [{'name': name, 'sha256': hashlib.sha256(body).hexdigest(),
                       'chunks': len(parsed[name])} for name, body in documents.items()],
    }
    if provenance:
        metadata['provenance'] = provenance
    return dataset['cases'], documents, metadata


def score_case(case: dict, hits: list[dict], top_k: int):
    if top_k < 1:
        raise ValueError('top_k 必须大于零。')
    selected = hits[:top_k]
    units = []
    for expected in case['evidence']:
        rank = next((rank for rank, hit in enumerate(selected, 1)
                     if hit['document_name'] == expected['document']
                     and expected['contains'] in hit['text']), None)
        units.append({**expected, 'rank': rank})
    ranks = [unit['rank'] for unit in units if unit['rank'] is not None]
    return {
        'id': case['id'], 'question': case['question'], 'split': case['split'],
        'category': case['category'], 'reason': case.get('reason'), 'evidence': units,
        'full_hit': len(ranks) == len(units) if units else None,
        'evidence_recall': len(ranks) / len(units) if units else None,
        'reciprocal_rank': 1 / min(ranks) if ranks else (0.0 if units else None),
        'returned_any': bool(selected),
        'sources': [{key: hit[key] for key in ('document_name', 'ordinal', 'text', 'score')}
                    for hit in selected],
    }


def aggregate(results: list[dict]):
    answerable = [r for r in results if r['category'] != 'unanswerable']
    negatives = [r for r in results if r['category'] == 'unanswerable']
    def mean_or_none(values):
        return statistics.mean(values) if values else None
    return {
        'case_count': len(results), 'answerable_count': len(answerable),
        'unanswerable_count': len(negatives),
        'full_evidence_hit_rate': mean_or_none([int(r['full_hit']) for r in answerable]),
        'mean_evidence_recall': mean_or_none([r['evidence_recall'] for r in answerable]),
        'mrr': mean_or_none([r['reciprocal_rank'] for r in answerable]),
        'unanswerable_retrieval_rate': mean_or_none([int(r['returned_any']) for r in negatives]),
    }


def grouped_metrics(results: list[dict]):
    groups = {'all': results}
    groups.update({split: [r for r in results if r['split'] == split]
                   for split in sorted({r['split'] for r in results})})
    return {name: {
        **aggregate(rows),
        'by_category': {category: aggregate([r for r in rows if r['category'] == category])
                        for category in sorted({r['category'] for r in rows})},
    } for name, rows in groups.items()}
