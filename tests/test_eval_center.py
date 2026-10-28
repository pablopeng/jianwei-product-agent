import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent import configuration as cfg
from agent.contracts import ROOT
from agent.runtime import SellingAgent
from agent.trajectory import review_trajectory, TRAJECTORY_RUBRIC
from agent.evaluation_store import save_record, load_records, statistics, get_record
from agent.evaluation_export import report_xml, export_lark

class EvaluationCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=patch.object(cfg,'STATE',Path(self.temp.name));self.state.start();self.addCleanup(self.state.stop)
        a=SellingAgent(json.loads((ROOT/'data/3825952452159603168.input.json').read_text()),None)
        a.knowledge=json.loads((ROOT/'examples/mahua.profile.json').read_text());a.result=json.loads((ROOT/'examples/mahua.points.json').read_text())
        self.session={'id':'a'*32,'owner':'owner-a','title':'测试商品','example':False,'agent':a,'ui_messages':[{'role':'user','text':'核查资料'}]}
    def score(self,score=1,rubric=None,example=False):
        self.session['example']=example
        rubric=rubric or cfg.documents()['evaluation']
        return save_record('owner-a',self.session,{'rubric':rubric,'scores':{d['id']:score for d in rubric['dimensions']},'quality_score':score,'score_max':2},'quality_score')
    def test_records_are_owner_scoped_and_snapshot_is_immutable(self):
        r=self.score();self.session['agent'].knowledge['product_info']['name']='后续改名'
        stored=get_record('owner-a',r['id'])
        self.assertNotEqual(stored['artifact']['profile']['product_info']['name'],'后续改名')
        self.assertEqual(load_records('owner-b'),[])
        with self.assertRaises(ValueError):get_record('owner-b',r['id'])
        with self.assertRaises(ValueError):get_record('owner-a','../x')
    def test_statistics_use_latest_and_keep_rubric_versions_separate(self):
        self.score(0);self.score(2)
        stats=statistics(load_records('owner-a'))
        self.assertEqual(stats['rated_results'],1)
        self.assertEqual(stats['groups'][0]['dimensions'][0]['mean'],2)
        rub=copy.deepcopy(cfg.documents()['evaluation']);rub['dimensions'][0]['definition']+='改版'
        self.score(1,rub)
        self.assertEqual(len(statistics(load_records('owner-a'))['groups']),2)
    def test_examples_are_excluded_and_suggestions_have_real_sample_counts(self):
        self.score(0,example=True)
        self.assertEqual(statistics(load_records('owner-a'))['rated_results'],0)
        included=statistics(load_records('owner-a'),True)
        self.assertEqual(included['rated_results'],1)
        self.assertTrue(all(s['n']==1 for s in included['suggestions']))
    def test_trajectory_never_passes_missing_confirmation_metadata(self):
        a=self.session['agent'];a.trace=[{'tool':'generate_selling_points','ok':True}]
        self.assertEqual(review_trajectory(a)['checks'][0]['status'],'review')
        a.trace=[{'tool':'generate_selling_points','ok':True,'confirmed_before':False,'profile_contract':2}]
        self.assertEqual(review_trajectory(a)['checks'][0]['status'],'failed')
    def test_duplicate_calls_only_count_within_same_turn(self):
        a=self.session['agent'];a.trace=[{'tool':'read_material','ok':True,'turn_id':str(i),'arguments':{'source_id':'same'}} for i in range(3)]
        self.assertEqual(review_trajectory(a)['metrics']['duplicate_excess'],0)
        for t in a.trace:t['turn_id']='one'
        self.assertEqual(review_trajectory(a)['metrics']['duplicate_excess'],1)
    def test_export_uses_argument_list_and_xml_escaping_and_reuses_link(self):
        r=self.score();r['session_title']='<script>bad</script>'
        xml=report_xml(r);self.assertIn('&lt;script&gt;',xml);self.assertNotIn('<script>',xml)
        fake=type('R',(),{'returncode':0,'stdout':json.dumps({'ok':True,'data':{'document':{'url':'https://example.feishu.cn/docx/test','document_id':'test'}}})})()
        with patch('agent.evaluation_export.subprocess.run',return_value=fake) as run:
            result=export_lark(r)
        self.assertEqual(run.call_args.args[0],['lark-cli','docs','+create','--content','-','--as','user'])
        self.assertIn('第一阶段商品档案快照',run.call_args.kwargs['input'])
        with patch('agent.evaluation_export.subprocess.run') as run:
            self.assertTrue(export_lark({**r,'lark_url':result['url']})['reused']);run.assert_not_called()
    def test_trajectory_scoring_is_independent_from_output_scoring(self):
        self.score(1)
        save_record('owner-a',self.session,{'rubric':TRAJECTORY_RUBRIC,'scores':{d['id']:2 for d in TRAJECTORY_RUBRIC['dimensions']},'quality_score':2,'score_max':2},'trajectory_score')
        groups=statistics(load_records('owner-a'))['groups']
        self.assertEqual({g['type'] for g in groups},{'quality_score','trajectory_score'})
