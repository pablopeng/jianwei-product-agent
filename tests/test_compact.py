import copy
import json
import unittest
from unittest.mock import patch

from agent.compact import material_batches, public, validate_points
from agent.contracts import ROOT, validate_shape, revision
from agent.runtime import SellingAgent
from test_agent import ScriptedClient


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / 'data/3825952452159603168.input.json').read_text())
        self.profile = json.loads((ROOT / 'examples/mahua.profile.json').read_text())
        self.result = json.loads((ROOT / 'examples/mahua.points.json').read_text())

    def test_output_has_only_points_and_priority(self):
        output = public(self.result)
        self.assertEqual(set(output), {'selling_points'})
        self.assertEqual(set(output['selling_points'][0]), {'text', 'priority'})
        validate_shape(output, 'selling-points.schema.json')

    def test_more_than_five_points_are_allowed(self):
        result = copy.deepcopy(self.result)
        result['selling_points'] = [{'text': '有依据的表达' + str(i), 'priority': i + 1} for i in range(8)]
        result['_meta']['support'] = [result['_meta']['support'][0]] * 8
        validate_points(result, self.profile)

    def test_batches_do_not_truncate_text_or_omit_images(self):
        raw = copy.deepcopy(self.raw)
        raw['materials'].append({**raw['materials'][0], 'id': 'long', 'text': '商品资料' * 20000})
        batches = material_batches(raw)
        self.assertGreater(len(batches), 1)
        flattened = [m for batch in batches for m in batch]
        self.assertEqual({m['id'] for m in flattened}, {m['id'] for m in raw['materials']})
        self.assertEqual(''.join(m['text'] for m in flattened if m['id'] == 'long'), '商品资料' * 20000)
        self.assertTrue(all(sum(m['kind'] == 'image' for m in batch) <= 10 for batch in batches))

    def test_summary_need_not_be_verbatim_and_marketing_is_reclassified(self):
        profile = public(self.profile)
        profile['product_info']['attributes'].append('香甜不腻的口感描述')
        sources = [{'item': item, 'source_ids': data['source_ids']} for item, data in self.profile['_meta']['sources'].items()]
        sources.append({'item': '香甜不腻的口感描述', 'source_ids': ['copy-19']})
        client = ScriptedClient([{'role': 'assistant', 'content': json.dumps({'profile': profile, 'sources': sources})}])
        agent = SellingAgent(self.raw, client)
        with patch('agent.compact.material_batches', return_value=[self.raw['materials']]):
            result = agent.normalize()
        self.assertNotIn('香甜不腻的口感描述', result['product_info']['attributes'])
        self.assertIn('香甜不腻的口感描述', result['original_selling_points'])
        self.assertEqual(len(client.requests), 1)

    def test_cautions_cannot_support_a_selling_point(self):
        result = copy.deepcopy(self.result)
        result['_meta']['support'][0]['basis'] = self.profile['cautions'][:1]
        with self.assertRaisesRegex(ValueError, '未确认'):
            validate_points(result, self.profile)

    def test_single_flavor_recipe_is_not_global_ingredient(self):
        raw = copy.deepcopy(self.raw)
        next(m for m in raw['materials'] if m['id'] == 'specs')['text'] = json.dumps({
            'child_spec_seq': ['口味分类'], 'spec_values_seq': [['牛肉大葱', '羊肉大葱']]})
        profile = public(self.profile)
        profile['product_info']['ingredients'] = ['牛肉〔attr-9〕']
        profile['specifications'] = [
            {'name': '牛肉大葱〔specs〕', 'details': ['配料：牛肉〔attr-9〕']},
            {'name': '羊肉大葱〔specs〕', 'details': []}]
        client = ScriptedClient([{'role': 'assistant', 'content': json.dumps(profile)}])
        agent = SellingAgent(raw, client)
        with patch('agent.compact.material_batches', return_value=[raw['materials']]):
            result = agent.normalize()
        self.assertEqual(result['product_info']['ingredients'], [])
        self.assertIn('牛肉', result['specifications'][0]['details'][0])

    def test_final_editor_can_remove_unsupported_draft_claim(self):
        client = ScriptedClient([
            {'role': 'assistant', 'content': json.dumps({'selling_points': [{'text': '全网唯一蜂蜜花生麻花〔F1〕', 'priority': 1}]})},
            {'role': 'assistant', 'content': json.dumps({'selling_points': [{'text': '蜂蜜花生麻花〔F1〕', 'priority': 1}]})}])
        agent = SellingAgent(self.raw, client)
        agent.knowledge = copy.deepcopy(self.profile)
        agent.confirm_profile(revision(agent.knowledge))
        result = agent.generate('生成卖点', [], '')
        self.assertNotIn('全网唯一', result['selling_points'][0]['text'])
        self.assertEqual(agent.result, result)
        self.assertTrue(result['_meta']['support'][0]['source_ids'])
