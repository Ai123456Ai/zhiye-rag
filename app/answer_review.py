"""Validate provenance of offline reviews, without pretending to judge semantics."""
import hashlib
import json


def response_hash(response):
    return hashlib.sha256(json.dumps(response, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_reviews(report, reviews):
    rows = {row['case']['id']: row for row in report['results'] if row.get('response')}
    if len(rows) != report['planned_count']:
        raise ValueError('运行未完成，不能生成完整评审结论。')
    if reviews.get('run_fingerprint') != report['fingerprint']:
        raise ValueError('评审与运行指纹不一致。')
    seen = set()
    for review in reviews['cases']:
        case_id = review['id']
        if case_id not in rows or case_id in seen:
            raise ValueError('评审题号未知或重复。')
        seen.add(case_id)
        response = rows[case_id]['response']
        if review['response_sha256'] != response_hash(response):
            raise ValueError(f'{case_id}: 回答或来源已变化。')
        negative = rows[case_id]['case']['category'] == 'unanswerable'
        allowed = {'not_applicable'} if negative else {'full', 'partial', 'incorrect', 'abstained'}
        if review['answer_coverage'] not in allowed:
            raise ValueError(f'{case_id}: 无效的覆盖标注。')
        if type(review.get('unsupported_claims_found')) is not bool:
            raise ValueError('未依据原文复核结论。')
        if review.get('citation_support') not in {'supported', 'issue', 'not_applicable'}:
            raise ValueError('无效引用支持标注。')
        if negative and type(review.get('correct_abstention')) is not bool:
            raise ValueError('无答案题须复核实际拒答。')
        if not negative and review.get('correct_abstention') is not None:
            raise ValueError('有答案题不进入无答案拒答指标。')
        if not review.get('notes') or not isinstance(review.get('support'), list):
            raise ValueError('评审须保留依据和说明。')
        if not review['support'] and (response['sources'] or review['citation_support'] != 'not_applicable'):
            raise ValueError('存在来源时须保留原文依据；无来源时引用支持应标为不适用。')
        by_citation = {s['citation']: s for s in response['sources']}
        for support in review['support']:
            source = by_citation.get(support['citation'])
            if not source or not support['quote'] or support['quote'] not in source['text']:
                raise ValueError(f'{case_id}: 评审依据不在实际发送的来源中。')
    if seen != set(rows):
        raise ValueError('缺少逐题评审。')


def summarize_reviews(report, reviews):
    validate_reviews(report, reviews)
    categories = {r['case']['id']: r['case']['category'] for r in report['results']}
    positives = [r for r in reviews['cases'] if categories[r['id']] != 'unanswerable']
    negatives = [r for r in reviews['cases'] if categories[r['id']] == 'unanswerable']
    return {
        'reviewer': reviews['reviewer'], 'reviewed_count': len(reviews['cases']),
        'answerable_count': len(positives), 'full_answer_count': sum(r['answer_coverage'] == 'full' for r in positives),
        'partial_answer_count': sum(r['answer_coverage'] == 'partial' for r in positives),
        'unanswerable_count': len(negatives),
        'correct_abstention_count': sum(r['correct_abstention'] for r in negatives),
        'answers_with_unsupported_claims_found': sum(r['unsupported_claims_found'] for r in reviews['cases']),
        'citation_support_issue_count': sum(r['citation_support'] == 'issue' for r in reviews['cases']),
        'limitation': '单次合成样本、Codex单评审；语义判断不是独立人工盲评，未做多评审一致性验证。',
    }
