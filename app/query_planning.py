"""Bounded, opt-in Chinese clause splitting; no model requests or inferred facts."""
import re

POLICY = 'rules_round_robin_v1'


def plan_queries(question: str, enabled: bool, top_k: int):
    plan = {'policy': POLICY, 'enabled': enabled, 'applied': False,
            'queries': [question], 'reason': 'disabled'}
    if not enabled:
        return plan
    literals = []

    def shield(match):
        literals.append(match.group())
        return f'\ue000{len(literals) - 1}\ue001'

    protected = re.sub(r'```[\s\S]*?```|`[^`]*`|"(?:\\.|[^"\\])*"|“[^”]*”', shield, question)
    parts = re.split(
        r'[？?](?=\s|[\u4e00-\u9fff]|$)|[；;]|'
        r'[，,]\s*(?:另外[，,]?|此外[，,]?|同时[，,]?|并且[，,]?|而)|'
        r'[，,]\s*(?=成功时|失败时)', protected)
    parts = [re.sub(r'^(?:另外|此外|同时)[，,\s]*', '', part.strip(' ，,。 \n')) for part in parts]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        plan['reason'] = 'no_safe_boundary'
        return plan
    if len(parts) > 3:
        plan['reason'] = 'too_many_parts'
        return plan
    queries = []
    for part in parts:
        if re.match(r'^(?:不是|不应|非要|且|是因为|是为了)', part):
            plan['reason'] = 'dependent_clause'
            return plan
        # Drop a shared deictic tail, without inventing missing subjects or conditions.
        part = re.sub(r'[，,](?:这两[处项种个]|两者|它们).{0,25}[？?]?$', '', part)
        for index, literal in enumerate(literals):
            part = part.replace(f'\ue000{index}\ue001', literal)
        if len(re.sub(r'\W', '', part)) < 6 or part in queries:
            plan['reason'] = 'short_or_repeated_clause'
            return plan
        queries.append(part)
    if top_k < len(queries):
        plan['reason'] = 'insufficient_top_k'
        return plan
    plan.update(applied=True, queries=[question, *queries], reason='explicit_boundaries')
    return plan


def interleave_rankings(rankings: list[list[tuple]], top_k: int):
    """Round robin subqueries first, original last; retain the choosing query index."""
    selected, seen = [], set()
    streams = [iter(ranking) for ranking in rankings]
    order = [*range(1, len(streams)), 0]
    while len(selected) < top_k:
        progress = False
        for index in order:
            for key, _ in streams[index]:
                if key in seen:
                    continue
                selected.append((key, index))
                seen.add(key)
                progress = True
                break
            if len(selected) == top_k:
                break
        if not progress:
            break
    return selected
