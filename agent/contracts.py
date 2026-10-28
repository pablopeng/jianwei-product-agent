"""JSON Schema校验与不可由模型绕过的来源/规格/版本约束。"""
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def schema(name):
    from .configuration import documents
    return documents()['schemas'][name]


def validate_shape(value, name):
    if name == 'product-knowledge.schema.json' and isinstance(value, dict) and 'sections' in value:
        Draft202012Validator(json.loads((ROOT / 'schemas/legacy-knowledge.schema.json').read_text())).validate(value)
        return
    if name == 'selling-points.schema.json' and isinstance(value, dict) and 'headline' in value:
        Draft202012Validator(json.loads((ROOT / 'schemas/legacy-selling-points.schema.json').read_text())).validate(value)
        return
    Draft202012Validator(schema(name)).validate(value)


def revision(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def observations(knowledge):
    return [item for section in knowledge['sections'].values() for item in section]


def unique(items, field, label):
    ids = [item[field] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError('重复的' + label)
    return set(ids)


def validate_knowledge(knowledge, raw, viewed_images=()):
    if 'product_info' in knowledge:
        from .compact import validate_profile
        return validate_profile(knowledge, raw)
    validate_shape(raw, 'input.schema.json')
    validate_shape(knowledge, 'product-knowledge.schema.json')
    if knowledge['product_id'] != raw['product_id'] or knowledge['material_revision'] != revision(raw):
        raise ValueError('商品或资料版本不一致')
    unique(raw['materials'], 'id', 'source_id')
    sources = {m['id']: m for m in raw['materials']}
    items = observations(knowledge)
    ids = unique(items, 'id', 'observation_id')
    sku_ids = unique(knowledge['skus'], 'id', 'sku_id')
    issue_ids = unique(knowledge['issues'], 'id', 'issue_id')
    unique(knowledge['questions'], 'id', 'question_id')

    def check_evidence(evidence):
        if evidence['source_id'] not in sources:
            raise ValueError('不存在的证据来源: ' + evidence['source_id'])
        material = sources[evidence['source_id']]
        if evidence['method'] == 'text':
            if evidence['quote'] not in material['text']:
                raise ValueError('证据不是来源原文: ' + evidence['source_id'])
        elif material['kind'] != 'image' or material['id'] not in viewed_images:
            raise ValueError('图片未实际提供给模型，不可声明已读图')

    for item in items:
        scope = item['scope']
        if not set(scope['sku_ids']) <= sku_ids:
            raise ValueError('信息引用不存在的SKU')
        if scope['kind'] == 'sku' and not scope['sku_ids']:
            raise ValueError('SKU范围不能为空')
        if scope['kind'] != 'sku' and scope['sku_ids']:
            raise ValueError('非SKU范围不能带SKU ID')
        for evidence in item['evidence']:
            check_evidence(evidence)
            origin = sources[evidence['source_id']]['origin']
            source_kind = sources[evidence['source_id']]['kind']
            if (origin == 'ai' or source_kind == 'ai_derived') and item['evidence_kind'] != 'ai_derived':
                raise ValueError('不能将旧AI材料升级为其他证据类型')
            if (origin == 'buyer' or source_kind == 'buyer_review') and item['evidence_kind'] != 'buyer_experience':
                raise ValueError('不能将买家评价升级为客观商品属性')
            if source_kind == 'marketing_copy' and item['evidence_kind'] != 'merchant_claim':
                raise ValueError('不能将宣传文案升级为客观商品属性')
    for sku in knowledge['skus']:
        for evidence in sku['evidence']:
            check_evidence(evidence)
    for evidence in knowledge['category']['evidence']:
        check_evidence(evidence)
    for issue in knowledge['issues']:
        if not set(issue['observation_ids']) <= ids or not set(issue['source_ids']) <= set(sources):
            raise ValueError('问题引用无效')
    for question in knowledge['questions']:
        if not set(question['related_issue_ids']) <= issue_ids:
            raise ValueError('问题关联的issue不存在')
    return knowledge


def validate_result(result, knowledge):
    if 'selling_points' in result:
        from .compact import validate_points
        return validate_points(result, knowledge)
    validate_shape(result, 'selling-points.schema.json')
    if result['product_id'] != knowledge['product_id']:
        raise ValueError('商品不匹配')
    if result['material_revision'] != knowledge['material_revision'] or result['knowledge_revision'] != revision(knowledge):
        raise ValueError('卖点基于过期的资料或商品档案')
    if knowledge['category']['industry'] != 'food_beverage':
        raise ValueError('当前Skill仅支持食品饮料')
    obs = {o['id']: o for o in observations(knowledge)}
    sku_ids = {s['id'] for s in knowledge['skus']}
    target = result['target']
    targets = set(target['sku_ids'])
    if target['scope'] == 'sku' and (not targets or not targets <= sku_ids):
        raise ValueError('请选择有效目标规格')
    if target['scope'] == 'common' and targets:
        raise ValueError('common模式不能携带特定SKU')
    if result['status'] == 'draft' and not result['points']:
        raise ValueError('没有卖点时不能标记为draft')
    if result['status'] == 'needs_input' and (result['points'] or result['headline'] or result['detail_copy'] or not result['questions']):
        raise ValueError('needs_input应留空文案并说明必要问题')
    points = result['points']
    unique(points, 'id', 'point_id')
    claims = [c for p in points for c in p['claims']]
    claim_ids = unique(claims, 'id', 'claim_id')
    if not set(result['headline_claim_ids']) <= claim_ids or not set(result['detail_claim_ids']) <= claim_ids:
        raise ValueError('标题或详情引用不存在的断言')
    if result['headline'] and not result['headline_claim_ids']:
        raise ValueError('主标题缺少断言关联')
    if result['detail_copy'] and not result['detail_claim_ids']:
        raise ValueError('详情说明缺少断言关联')
    conflicted = {oid for issue in knowledge['issues'] if issue['kind'] == 'conflict' and issue['resolution'] != 'resolved' for oid in issue['observation_ids']}
    permitted = {
        'product_fact': {'explicit_attribute', 'visual_observation'},
        'merchant_description': {'merchant_claim'},
        'buyer_feedback': {'buyer_experience'},
        'usage_suggestion': {'explicit_attribute', 'visual_observation'},
    }
    for point in points:
        if set(point['sku_ids']) != targets:
            raise ValueError('卖点范围须与本次目标规格一致')
        for claim in point['claims']:
            for oid in claim['observation_ids']:
                if oid not in obs:
                    raise ValueError('卖点引用不存在的信息: ' + oid)
                item = obs[oid]
                if item['status'] != 'supported' or oid in conflicted:
                    raise ValueError('卖点引用了未确认或冲突信息: ' + oid)
                if item['evidence_kind'] not in permitted[claim['kind']]:
                    raise ValueError('证据类型不能支持该断言: ' + oid)
                scope = item['scope']
                if scope['kind'] == 'unknown':
                    raise ValueError('不能使用适用规格不明的信息')
                if scope['kind'] == 'sku' and (not targets or not targets <= set(scope['sku_ids'])):
                    raise ValueError('卖点混用了其他规格的信息')
    for omission in result['omissions']:
        if not set(omission['observation_ids']) <= set(obs) or not set(omission['issue_ids']) <= {i['id'] for i in knowledge['issues']}:
            raise ValueError('省略说明引用无效')
    return result
