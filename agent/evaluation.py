"""跨行业评审：底线与质量分离，机评逐条回到原始资料。"""
import json
from .compact import public, leaves, material_batches
from .contracts import revision


def validate_rubric(rubric):
    if rubric.get('score_range', [1, 5]) != [0, 2]:
        raise ValueError('本版评测采用0/1/2三档')
    entries = rubric.get('dimensions', []) + rubric.get('gates', [])
    ids = [item.get('id') for item in entries]
    if len(ids) != len(set(ids)):
        raise ValueError('评测维度ID不能重复')
    for item in entries:
        if not isinstance(item.get('definition'), str) or not item['definition'].strip():
            raise ValueError('每个维度需要具体定义')
        anchors = item.get('anchors', {})
        if set(anchors) != {'0', '1', '2'} or any(not isinstance(v, str) or not v.strip() for v in anchors.values()):
            raise ValueError('每个维度需要0、1、2分定义')


def score_report(scores, gates, rubric):
    dims, bottom = rubric['dimensions'], rubric.get('gates', [])
    def validate(values, entries):
        return (isinstance(values, dict) and set(values) == {d['id'] for d in entries}
                and all(type(v) is int and v in [0, 1, 2] for v in values.values()))
    if not validate(scores, dims):
        raise ValueError('请为每个质量维度填写0、1或2分，不能留空')
    if gates and not validate(gates, bottom):
        raise ValueError('请完成全部底线评分，分数为0、1或2')
    status = 'unreviewed' if not gates else 'failed' if 0 in gates.values() else 'review' if 1 in gates.values() else 'passed'
    weighted = round(sum(scores[d['id']] * d['weight'] for d in dims) / sum(d['weight'] for d in dims), 2)
    return {'scores': scores, 'gate_scores': gates, 'gate_status': status, 'score_max': 2,
            'quality_score': weighted, 'weighted_score': weighted,
            'decision': '底线通过，质量分供比较' if status == 'passed' else '底线未通过或尚待复核，质量分不能视为整体通过'}


def accuracy_review(agent, notify=lambda message: None):
    """核查profile与result全部业务断言；供应商失败不伪造通过。"""
    profile, result = agent.knowledge, agent.result
    if not profile or (result and 'selling_points' not in result):
        raise ValueError('请先整理当前版本的商品档案')
    result = result or {}
    claims = []
    for i, text in enumerate(dict.fromkeys(leaves({k: v for k, v in public(profile).items() if k != 'cautions'}))):
        claims.append({'id': 'profile-' + str(i + 1), 'stage': 'profile', 'text': text,
                       'source_ids': profile['_meta']['sources'].get(text, {}).get('source_ids', [])})
    for i, point in enumerate(result.get('selling_points', [])):
        claims.append({'id': 'point-' + str(i + 1), 'stage': 'selling_points', 'text': point['text'],
                       'source_ids': result['_meta']['support'][i]['source_ids']})
    images, _ = agent.image_blocks()
    image_map = {images[i]['text'].split('=', 1)[1]: images[i:i + 2] for i in range(0, len(images), 2)}
    findings = {c['id']: [] for c in claims}
    from .configuration import ROOT
    prompt = (ROOT / 'prompts/accuracy.md').read_text()
    batches = material_batches(agent.raw)
    for number, batch in enumerate(batches, 1):
        ids = {m['id'] for m in batch}
        selected = [c for c in claims if ids.intersection(c['source_ids'])]
        if not selected:
            continue
        notify('核对原始资料 ' + str(number) + '/' + str(len(batches)))
        payload = {'claims': selected, 'product_context': public(profile), 'materials': [
            {k: m[k] for k in ['id', 'kind', 'origin', 'label', 'text']} for m in batch]}
        blocks = [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}]
        for m in batch:
            blocks.extend(image_map.get(m['id'], []))
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': blocks}]
        for attempt in range(2):
            response = agent.client.complete(messages, structured=True, vision=any(m['id'] in image_map for m in batch))
            try:
                rows = json.loads(response.get('content') or '{}').get('claims', [])
                if (not isinstance(rows, list) or len(rows) != len(selected)
                        or {r.get('id') for r in rows} != {c['id'] for c in selected}):
                    raise ValueError('每个输入id必须恰好返回一次')
                for row in rows:
                    if row.get('verdict') not in ['supported', 'contradicted', 'insufficient']:
                        raise ValueError('verdict只能是supported、contradicted或insufficient三个英文值之一')
                    if (not isinstance(row.get('evidence', []), list)
                            or any(not isinstance(e, dict) for e in row.get('evidence', []))):
                        raise ValueError('evidence必须是来源对象数组')
                break
            except (ValueError, TypeError, AttributeError) as exc:
                if attempt:
                    raise ValueError('机评结构未通过校验，未记为通过；请重试。') from None
                messages.extend([{'role': 'assistant', 'content': response.get('content') or ''},
                    {'role': 'user', 'content': '只修复返回JSON，不改变输入。' + str(exc)}])
        sources = {m['id']: m for m in batch}
        for row in rows:
            if row.get('verdict') not in ['supported', 'contradicted', 'insufficient']:
                raise ValueError('机评返回了未知判断')
            evidence = row.get('evidence', [])
            valid = bool(evidence)
            for e in evidence:
                source = sources.get(e.get('source_id'))
                quote = e.get('quote', '')
                if (not source or source['origin'] == 'ai' or source['kind'] == 'ai_derived'
                        or not isinstance(quote, str) or not quote.strip()
                        or (source['kind'] != 'image' and quote not in source['text'])):
                    valid = False
            if row['verdict'] != 'insufficient' and not valid:
                row = {**row, 'verdict': 'insufficient', 'reason': '引用未通过原文/来源检查，需要复核。'}
            findings[row['id']].append(row)
    report = []
    for claim in claims:
        records = findings[claim['id']]
        status = ('contradicted' if any(r['verdict'] == 'contradicted' for r in records)
                  else 'supported' if any(r['verdict'] == 'supported' for r in records) else 'insufficient')
        report.append({**claim, 'verdict': status, 'reviews': records})
    counts = {v: sum(c['verdict'] == v for c in report) for v in ['supported', 'contradicted', 'insufficient']}
    return {'type': 'accuracy', 'product_id': agent.raw['product_id'], 'result_revision': revision(result) if result else None,
            'profile_revision': revision(profile), 'material_revision': revision(agent.raw),
            'counts': counts, 'claims': report,
            'checks': [{'name': '原始资料 → 档案 → 卖点语义核查',
                        'status': 'failed' if counts['contradicted'] else 'review' if counts['insufficient'] else 'passed',
                        'detail': f"有据 {counts['supported']} / 冲突 {counts['contradicted']} / 待核 {counts['insufficient']}"}],
            'note': '模型辅助核查，逐条保留来源。未发现错误不代表事实已获独立验证；不能衡量转化效果。'}
