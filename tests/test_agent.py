import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from agent.contracts import ROOT, revision, schema, validate_knowledge, validate_result, validate_shape
from agent.runtime import SellingAgent, DeepSeekClient


def read(path):
    return json.loads((ROOT / path).read_text())


def call(name, args, id='call1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class ScriptedClient:
    """只验证工具循环和状态约束，不代表真实模型能力或效果评测。"""
    def __init__(self, messages):
        self.responses = list(messages)
        self.requests = []

    def complete(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.raw = read('data/3825952452159603168.input.json')
        self.knowledge = read('examples/mahua.knowledge.json')
        self.result = read('examples/mahua.selling-points.json')
        self.viewed = {m['id'] for m in self.raw['materials'] if m['image_path']}

    def test_schemas_and_all_14_inputs(self):
        for name in ['input.schema.json', 'product-knowledge.schema.json', 'selling-points.schema.json']:
            Draft202012Validator.check_schema(schema(name))
        products = read('data/manifest.json')['products']
        self.assertEqual(len(products), 14)
        for product in products:
            validate_shape(read(product['input_file']), 'input.schema.json')

    def test_real_material_example(self):
        validate_knowledge(self.knowledge, self.raw, self.viewed)
        validate_result(self.result, self.knowledge)

    def test_nonexistent_source_is_rejected(self):
        self.knowledge['sections']['product_attributes'][0]['evidence'][0]['source_id'] = 'invented'
        with self.assertRaisesRegex(ValueError, '不存在'):
            validate_knowledge(self.knowledge, self.raw, self.viewed)

    def test_fabricated_quote_is_rejected(self):
        self.knowledge['sections']['product_attributes'][0]['evidence'][0]['quote'] = '没有这句话'
        with self.assertRaisesRegex(ValueError, '原文'):
            validate_knowledge(self.knowledge, self.raw, self.viewed)

    def test_unread_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '未实际'):
            validate_knowledge(self.knowledge, self.raw, set())

    def test_buyer_review_cannot_be_laundered_into_fact(self):
        self.knowledge['sections']['buyer_reviews'][0]['evidence_kind'] = 'explicit_attribute'
        with self.assertRaisesRegex(ValueError, '买家评价'):
            validate_knowledge(self.knowledge, self.raw, self.viewed)

    def test_marketing_cannot_be_laundered_into_fact(self):
        self.knowledge['sections']['marketing_claims'][0]['evidence_kind'] = 'explicit_attribute'
        with self.assertRaisesRegex(ValueError, '宣传文案'):
            validate_knowledge(self.knowledge, self.raw, self.viewed)

    def test_ai_material_cannot_be_laundered_into_fact(self):
        item = self.knowledge['sections']['product_attributes'][0]
        source = next(m for m in self.raw['materials'] if m['origin'] == 'ai')
        item['evidence'] = [{'source_id': source['id'], 'quote': source['text'][:4], 'locator': source['locator'], 'method': 'text'}]
        with self.assertRaisesRegex(ValueError, '旧AI'):
            validate_knowledge(self.knowledge, self.raw, self.viewed)

    def test_conflicted_weight_cannot_support_result(self):
        self.result['points'][0]['claims'][0]['observation_ids'] = ['f3']
        with self.assertRaisesRegex(ValueError, '冲突'):
            validate_result(self.result, self.knowledge)

    def test_unknown_scope_cannot_support_result(self):
        self.knowledge['sections']['product_attributes'][0]['scope']['kind'] = 'unknown'
        self.result['knowledge_revision'] = revision(self.knowledge)
        with self.assertRaisesRegex(ValueError, '规格不明'):
            validate_result(self.result, self.knowledge)

    def test_sku_specific_claim_cannot_be_used_for_all_skus(self):
        item = self.knowledge['sections']['product_attributes'][0]
        item['scope'] = {'kind': 'sku', 'sku_ids': ['sku-1']}
        self.result['knowledge_revision'] = revision(self.knowledge)
        with self.assertRaisesRegex(ValueError, '其他规格'):
            validate_result(self.result, self.knowledge)

    def test_headline_must_reference_existing_claim(self):
        self.result['headline_claim_ids'] = ['invented']
        with self.assertRaisesRegex(ValueError, '断言'):
            validate_result(self.result, self.knowledge)

    def test_new_material_invalidates_knowledge(self):
        agent = SellingAgent(self.raw, ScriptedClient([]))
        agent.knowledge = self.knowledge
        agent.add_material({'id': 'new', 'kind': 'merchant_supplement', 'label': '确认',
            'locator': 'user', 'text': '当前规格净含量278克', 'image_path': '', 'image_token': '', 'origin': 'merchant'})
        with self.assertRaisesRegex(ValueError, '过期'):
            agent.generate('重写', [], '')

    def test_result_invalid_after_knowledge_changes(self):
        self.knowledge['questions'].append({'id': 'q1', 'question': '确认规格？', 'reason': '待确认',
            'required_for': 'sku_selection', 'related_issue_ids': []})
        with self.assertRaisesRegex(ValueError, '过期'):
            validate_result(self.result, self.knowledge)

    def test_lookup_question_does_not_force_generation(self):
        client = ScriptedClient([call('search_materials', {'query': '配料', 'offset': 0}),
            call('read_material', {'source_id': 'specs', 'offset': 0}, 'call2'),
            {'role': 'assistant', 'content': '已找到配料与规格材料。'}])
        agent = SellingAgent(self.raw, client)
        response = agent.turn('只查一下配料和规格')
        self.assertEqual(response['status'], 'answered')
        self.assertEqual([t['tool'] for t in agent.trace], ['search_materials', 'read_material'])
        self.assertIsNone(agent.knowledge)
        self.assertIsNone(agent.result)

    def test_clarification_waits_and_resumes(self):
        client = ScriptedClient([call('ask_clarification', {'question': '你想突出哪种规格？', 'reason': '规格不同'}),
            {'role': 'assistant', 'content': '收到，按你选的规格继续。'}])
        agent = SellingAgent(self.raw, client)
        self.assertEqual(agent.turn('帮我写')['status'], 'waiting_for_user')
        state = agent.snapshot()
        resumed = SellingAgent.restore(state, client)
        self.assertEqual(resumed.turn('第一种')['status'], 'answered')
        self.assertEqual(len(client.requests), 2)

    def test_tool_error_can_be_recovered(self):
        client = ScriptedClient([call('read_material', {'source_id': 'invalid', 'offset': 0}),
            call('list_materials', {}, 'call2'), {'role': 'assistant', 'content': '已重新找到资料。'}])
        agent = SellingAgent(self.raw, client)
        self.assertEqual(agent.turn('查资料')['status'], 'answered')
        self.assertFalse(agent.trace[0]['ok'])
        self.assertTrue(agent.trace[1]['ok'])

    def test_step_budget_stops_loop(self):
        agent = SellingAgent(self.raw, ScriptedClient([call('list_materials', {}),
            {'role': 'assistant', 'content': '已查到资料目录，尚未核实具体字段。'}]), max_steps=1)
        response = agent.turn('查资料')
        self.assertEqual(response['status'], 'partial')
        self.assertIn('尚未核实', response['message'])

    def test_bulk_read_avoids_serial_attribute_calls(self):
        agent = SellingAgent(self.raw, ScriptedClient([]))
        result = agent.dispatch('read_materials', {'source_ids': ['identity', 'specs']})
        self.assertEqual(len(result['materials']), 2)

    def test_conflict_check_uses_one_inspection_tool(self):
        client = ScriptedClient([call('inspect_product', {'focus': '检查冲突'}),
            {'role': 'assistant', 'content': '## 核查\n278克与300克待确认。'},
            {'role': 'assistant', 'content': '## 核查结论\n克重需要确认。'}])
        agent = SellingAgent(self.raw, client)
        response = agent.turn('检查商品信息冲突')
        self.assertEqual(response['status'], 'answered')
        self.assertEqual([t['tool'] for t in agent.trace], ['inspect_product'])
        self.assertTrue(any(part.get('type') == 'image_url' for part in client.requests[1][-1]['content']))

    def test_all_14_samples_have_readable_images(self):
        for product in read('data/manifest.json')['products']:
            raw = read(product['input_file'])
            images = [m for m in raw['materials'] if m['kind'] == 'image']
            self.assertTrue(images)
            self.assertTrue(all(m['image_path'] and (ROOT / m['image_path']).is_file() for m in images))

    def test_stream_parser_merges_tools_and_ignores_hidden_reasoning(self):
        chunks = [
            {'choices': [{'delta': {'reasoning_content': '不应展示的内容'}, 'finish_reason': None}]},
            {'choices': [{'delta': {'content': '开始'}, 'finish_reason': None}]},
            {'choices': [{'delta': {'content': '核查', 'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'inspect_', 'arguments': '{"focus":'}}]}, 'finish_reason': None}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'product', 'arguments': '"冲突"}'}}]}, 'finish_reason': 'tool_calls'}]},
        ]
        seen = []
        lines = [('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n').encode() for chunk in chunks]
        response = DeepSeekClient.parse_stream(lines + [b'data: [DONE]\n'], seen.append)
        self.assertEqual(seen, ['开始', '核查'])
        self.assertNotIn('reasoning_content', response)
        self.assertEqual(response['tool_calls'][0]['function']['name'], 'inspect_product')
        self.assertEqual(json.loads(response['tool_calls'][0]['function']['arguments']), {'focus': '冲突'})

    def test_interrupted_provider_stream_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, '中断'):
            DeepSeekClient.parse_stream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'])

    def test_exact_user_material_only(self):
        agent = SellingAgent(self.raw, ScriptedClient([]))
        agent.messages.append({'role': 'user', 'content': '我确认当前规格是278g。'})
        with self.assertRaisesRegex(ValueError, '原话'):
            agent.dispatch('record_user_material', {'exact_quote': '低糖', 'label': '属性'})
        result = agent.dispatch('record_user_material', {'exact_quote': '当前规格是278g', 'label': '规格确认'})
        self.assertTrue(result['knowledge_stale'])

    def test_partial_rewrite_preserves_other_points(self):
        rewritten = copy.deepcopy(self.result)
        rewritten['points'][1]['body'] = '独立小包，可放办公室分享。'
        client = ScriptedClient([{'role': 'assistant', 'content': json.dumps(rewritten, ensure_ascii=False)}])
        agent = SellingAgent(self.raw, client)
        agent.knowledge, agent.result = self.knowledge, self.result
        response = agent.generate('把第二条写短一点', [], 'p2')
        self.assertEqual(response['points'][0], self.result['points'][0])
        self.assertNotEqual(response['points'][1]['body'], self.result['points'][1]['body'])

    def test_bad_partial_rewrite_is_not_saved(self):
        rewritten = copy.deepcopy(self.result)
        rewritten['points'][0]['body'] = '被意外改动'
        client = ScriptedClient([{'role': 'assistant', 'content': json.dumps(rewritten)}] * 2)
        agent = SellingAgent(self.raw, client)
        agent.knowledge, agent.result = self.knowledge, self.result
        with self.assertRaisesRegex(ValueError, '非目标'):
            agent.generate('只改第二条', [], 'p2')
        self.assertEqual(agent.result, self.result)

    def test_end_to_end_tool_protocol_with_scripted_model(self):
        profile = read('examples/mahua.profile.json')
        result = read('examples/mahua.points.json')
        profile_envelope = {'profile': {k: v for k, v in profile.items() if k != '_meta'},
            'sources': [{'item': item, 'source_ids': data['source_ids']} for item, data in profile['_meta']['sources'].items()]}
        result_envelope = {'output': {'selling_points': result['selling_points']}, 'support': result['_meta']['support']}
        client = ScriptedClient([
            call('normalize_product', {}),
            {'role': 'assistant', 'content': json.dumps(profile_envelope, ensure_ascii=False)},
            call('confirm_product_profile', {'exact_quote': '确认商品档案'}),
            call('generate_selling_points', {'instruction': '先避开克重生成两条', 'target_sku_ids': [], 'focus_point_id': ''}, 'call2'),
            {'role': 'assistant', 'content': json.dumps(result_envelope, ensure_ascii=False)},
            {'role': 'assistant', 'content': json.dumps(result_envelope, ensure_ascii=False)},
            {'role': 'assistant', 'content': '已生成两条卖点，省略尚未确认的克重。'}])
        agent = SellingAgent(self.raw, client)
        with patch('agent.compact.material_batches', return_value=[self.raw['materials']]):
            first = agent.turn('先避开克重生成两条')
            self.assertEqual(first['status'], 'awaiting_profile_confirmation')
            self.assertIsNone(first['result'])
            response = agent.turn('确认商品档案，先避开克重生成两条')
        self.assertEqual(response['result']['selling_points'], result['selling_points'])
        self.assertEqual([t['tool'] for t in agent.trace], ['normalize_product', 'confirm_product_profile', 'generate_selling_points'])
        self.assertTrue(all(t['ok'] for t in agent.trace))

    def test_no_more_tools_after_clarification_in_same_batch(self):
        message = call('ask_clarification', {'question': '请确认目标规格', 'reason': '规格混合'})
        message['tool_calls'] += call('normalize_product', {}, 'call2')['tool_calls']
        agent = SellingAgent(self.raw, ScriptedClient([message]))
        response = agent.turn('生成卖点')
        self.assertEqual(response['status'], 'waiting_for_user')
        self.assertIsNone(agent.knowledge)
        self.assertFalse(agent.trace[-1]['ok'])


if __name__ == '__main__':
    unittest.main()
