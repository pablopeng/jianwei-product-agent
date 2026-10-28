import copy
import json
import unittest
from unittest.mock import patch
from agent.configuration import documents
from agent.evaluation import score_report, validate_rubric, accuracy_review
from agent.contracts import ROOT
from agent.runtime import SellingAgent

class Judge:
    def complete(self, messages, **kwargs):
        payload=json.loads(messages[1]['content'][0]['text'])
        return {'content': json.dumps({'claims':[{'id':c['id'],'verdict':'supported','reason':'测试',
            'evidence':[{'source_id':'attr-1','quote':'虚构的证据'}]} for c in payload['claims']]})}

class EvaluationTests(unittest.TestCase):
    def setUp(self): self.rubric=documents()['evaluation']
    def test_zero_is_valid_and_gates_cannot_be_offset(self):
        scores={d['id']:2 for d in self.rubric['dimensions']}
        gates={d['id']:2 for d in self.rubric['gates']};gates['factual']=0
        report=score_report(scores,gates,self.rubric)
        self.assertEqual(report['quality_score'],2)
        self.assertEqual(report['gate_status'],'failed')
        scores['hook']=0
        self.assertLess(score_report(scores,gates,self.rubric)['quality_score'],2)
    def test_missing_scores_are_not_treated_as_zero(self):
        with self.assertRaises(ValueError):score_report({}, {}, self.rubric)
        scores={d['id']:1 for d in self.rubric['dimensions']}
        with self.assertRaises(ValueError):score_report(scores,{'factual':2},self.rubric)
        self.assertEqual(score_report(scores,{},self.rubric)['gate_status'],'unreviewed')
        scores['hook']=True
        with self.assertRaises(ValueError):score_report(scores,{},self.rubric)
    def test_rubric_requires_three_anchors(self):
        r=copy.deepcopy(self.rubric);del r['dimensions'][0]['anchors']['0']
        with self.assertRaises(ValueError):validate_rubric(r)
    def test_machine_judge_cannot_pass_with_invented_quote(self):
        raw=json.loads((ROOT/'data/3825952452159603168.input.json').read_text())
        a=SellingAgent(raw,Judge());a.knowledge=json.loads((ROOT/'examples/mahua.profile.json').read_text())
        a.result=json.loads((ROOT/'examples/mahua.points.json').read_text())
        with patch('agent.evaluation.material_batches',return_value=[raw['materials']]),patch.object(a,'image_blocks',return_value=([],[])):
            report=accuracy_review(a)
        self.assertEqual(report['counts']['supported'],0)
        self.assertEqual(report['counts']['insufficient'],len(report['claims']))
        self.assertTrue(any(c['stage']=='profile' for c in report['claims']))
        self.assertTrue(any(c['stage']=='selling_points' for c in report['claims']))
    def test_editing_generation_criteria_invalidates_old_results(self):
        from agent import configuration as cfg
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, patch.object(cfg,'STATE',Path(folder)):
            before=cfg.documents()['revision']
            r=copy.deepcopy(cfg.documents()['evaluation']);r['dimensions'][0]['definition']+=' 新要求'
            cfg.save_document('evaluation','',r)
            self.assertNotEqual(cfg.documents()['revision'],before)
    def test_first_stage_can_be_reviewed_without_selling_points(self):
        raw=json.loads((ROOT/'data/3825952452159603168.input.json').read_text())
        a=SellingAgent(raw,Judge());a.knowledge=json.loads((ROOT/'examples/mahua.profile.json').read_text())
        with patch('agent.evaluation.material_batches',return_value=[raw['materials']]),patch.object(a,'image_blocks',return_value=([],[])):
            report=accuracy_review(a)
        self.assertIsNone(report['result_revision'])
        self.assertTrue(all(c['stage']=='profile' for c in report['claims']))
