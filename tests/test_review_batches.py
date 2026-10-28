import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent import configuration as cfg
from agent.contracts import ROOT
from agent.runtime import SellingAgent
from agent import review_batches as rb

class BatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(cfg,'STATE',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        a=SellingAgent(json.loads((ROOT/'data/3825952452159603168.input.json').read_text()),None)
        a.knowledge=json.loads((ROOT/'examples/mahua.profile.json').read_text());a.result=json.loads((ROOT/'examples/mahua.points.json').read_text())
        self.s={'id':'a'*32,'title':'商品','owner':'owner','agent':a,'example':True,'ui_messages':[]}
        self.b=rb.create_batch('owner',[self.s],'单元测试批次')
    def matrix(self):
        p,_,_=rb.build_workbook(self.b);s=p['sheets'][0];return [s['columns']]+s['data']
    def test_first_three_columns_and_blank_scores_and_source_preservation(self):
        p,_,_=rb.build_workbook(self.b)
        self.assertEqual(p['sheets'][0]['columns'][:3],['原始商品信息','一阶段商品依据','二阶段卖点'])
        self.assertTrue(all(v=='' for v in p['sheets'][0]['data'][0][3:19]))
        material=self.s['agent'].raw['materials'][0]
        source=p['sheets'][2]
        self.assertIn(material['text'],[r[4] for r in source['data']])
        self.assertEqual(self.b['rubric']['version'],'universal-selling-points-2.0')
    def test_blank_is_unrated_and_zero_is_a_real_score(self):
        m=self.matrix();r,issues=rb.parse_ratings(self.b,m);self.assertFalse(issues)
        self.assertEqual(next(iter(r.values()))['status'],'unrated')
        for i in range(8):m[1][3+2*i]='0'
        r,_=rb.parse_ratings(self.b,m);self.assertEqual(next(iter(r.values()))['status'],'complete')
        self.assertEqual(next(iter(r.values()))['scores']['hook'],0)
    def test_illegal_score_is_not_coerced_to_zero(self):
        m=self.matrix();m[1][3]='3';r,issues=rb.parse_ratings(self.b,m)
        self.assertTrue(issues);self.assertNotIn('factual',next(iter(r.values()))['gate_scores'])
    def test_missing_row_duplicate_or_changed_artifact_is_rejected(self):
        m=self.matrix()
        with self.assertRaises(ValueError):rb.parse_ratings(self.b,m[:1])
        with self.assertRaises(ValueError):rb.parse_ratings(self.b,m+[m[1]])
        m[1][2]='被改写的卖点'
        with self.assertRaisesRegex(ValueError,'被改写'):rb.parse_ratings(self.b,m)
    def test_frozen_snapshot_and_owner_and_reorder(self):
        self.s['agent'].result['selling_points'][0]['text']='之后改写'
        self.assertNotEqual(self.b['samples'][0]['result']['selling_points'][0]['text'],'之后改写')
        with self.assertRaises(ValueError):rb.load_batch('other',self.b['id'])
    def test_partial_scores_analysis_uses_actual_cell_count(self):
        m=self.matrix();m[1][9]=0;m[1][10]='漏了包装理由'
        r,_=rb.parse_ratings(self.b,m);self.b['ratings']=r;a=rb.analyze(self.b)
        self.assertEqual(a['dimensions'][0]['n'],1);self.assertEqual(a['dimensions'][1]['n'],0)
        self.assertEqual(a['suggestions'][0]['reviewer_notes'],['漏了包装理由'])
    def test_import_blank_does_not_create_fake_manual_scores(self):
        m=self.matrix();n=len(m[0]);data={'has_more':False,'warning_message':'定位说明','ranges':[{'actual_range':'A1:AB2','row_indices':[1,2],'col_indices':[rb.col(i) for i in range(n)],'cells':[[{'value':v} for v in row] for row in m]}]}
        self.b.update(export_status='ready',sheet_token='token',sheet_id='sid')
        with patch.object(rb,'cli',return_value=data):report=rb.import_batch(self.b)
        self.assertEqual(report['progress']['unrated'],1)
        self.assertEqual(list((cfg.STATE/'scores').glob('*.json')),[])
    def test_truncated_sheet_never_imported(self):
        with self.assertRaises(ValueError):rb.decode_cells({'has_more':True})

    def test_dropdown_readback_requires_every_score_cell_and_exact_options(self):
        data={'has_more':False,'warning_message':'定位','ranges':[{'actual_range':'D2:R2','row_indices':[2],'col_indices':[rb.col(i) for i in range(3,18)],'cells':[[{'data_validation':{'type':'list','items':['0','1','2']}} if i%2==0 else {} for i in range(15)]]}]}
        rb.verify_dropdowns(data,2,8)
        data['ranges'][0]['cells'][0][0]['data_validation']['items']=['1','2']
        with self.assertRaises(RuntimeError):rb.verify_dropdowns(data,2,8)
    def test_complete_import_is_saved_once_when_unchanged(self):
        m=self.matrix()
        for i in range(8):m[1][3+2*i]='2'
        data={'has_more':False,'warning_message':'定位','ranges':[{'actual_range':'A1:AB2','row_indices':[1,2],'col_indices':[rb.col(i) for i in range(len(m[0]))],'cells':[[{'value':v} for v in row] for row in m]}]}
        self.b.update(export_status='ready',sheet_token='token',sheet_id='sid')
        with patch.object(rb,'cli',return_value=data),patch.object(rb,'save_record') as save:
            report=rb.import_batch(self.b);rb.import_batch(self.b)
        self.assertEqual(report['progress']['complete'],1);self.assertEqual(save.call_count,1)
