import copy
import json
import unittest
from unittest.mock import patch
from agent.runtime import SellingAgent
from agent.contracts import ROOT, revision
from agent.compact import public
from test_agent import ScriptedClient

class ProfileReviewTests(unittest.TestCase):
    def setUp(self):
        self.raw=json.loads((ROOT/'data/3825952452159603168.input.json').read_text())
        self.profile=json.loads((ROOT/'examples/mahua.profile.json').read_text())
        self.agent=SellingAgent(self.raw,None);self.agent.knowledge=copy.deepcopy(self.profile)
    def test_generation_requires_current_confirmation(self):
        with self.assertRaisesRegex(ValueError,'尚未确认'):self.agent.generate('生成',[],'')
        self.agent.confirm_profile(revision(self.agent.knowledge))
        self.assertTrue(self.agent.profile_confirmed())
        self.agent.add_material({**self.raw['materials'][0],'id':'new-user-fact','text':'补充新规格'})
        self.assertFalse(self.agent.profile_confirmed())
    def test_edit_records_versions_and_invalidates_confirmation(self):
        a=self.agent;a.confirm_profile(revision(a.knowledge));before=revision(a.knowledge)
        p=public(copy.deepcopy(a.knowledge));p['product_info']['name']='商家更正商品名'
        a.edit_profile(p,before)
        self.assertFalse(a.profile_confirmed());self.assertEqual(len(a.profile_history),2)
        self.assertEqual(a.profile_history[0]['profile']['product_info']['name'],self.profile['product_info']['name'])
        sid=a.knowledge['_meta']['sources']['商家更正商品名']['source_ids'][0]
        self.assertTrue(sid.startswith('edit-'))
        self.assertEqual(a.knowledge['_meta']['sources']['独立小包装'],self.profile['_meta']['sources']['独立小包装'])
        with self.assertRaisesRegex(ValueError,'已更新'):a.edit_profile(p,before)
        a.confirm_profile(revision(a.knowledge))
        restored=SellingAgent.restore(a.snapshot(),None)
        self.assertTrue(restored.profile_confirmed());self.assertEqual(len(restored.profile_history),2)
    def test_natural_revision_uses_user_quote_and_requires_reconfirmation(self):
        a=self.agent;a.confirm_profile(revision(a.knowledge))
        p=public(copy.deepcopy(a.knowledge));p['original_selling_points']=[]
        a.client=ScriptedClient([{'role':'assistant','content':json.dumps(p)}])
        a.messages.append({'role':'user','content':'请删除档案中原有卖点，其他不变'})
        a.dispatch('revise_product_profile',{'exact_quote':'请删除档案中原有卖点，其他不变'})
        self.assertFalse(a.profile_confirmed());self.assertEqual(a.knowledge['original_selling_points'],[])
        self.assertEqual(a.raw['materials'][-1]['text'],'请删除档案中原有卖点，其他不变')
    def test_cannot_confirm_profile_created_in_same_turn(self):
        a=self.agent;a.messages.append({'role':'user','content':'确认并生成'})
        a.profile_at_turn_start=None
        with self.assertRaisesRegex(ValueError,'先查看'):a.dispatch('confirm_product_profile',{'exact_quote':'确认并生成'})
