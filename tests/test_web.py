import base64
import copy
import http.cookiejar
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from PIL import Image

from agent import configuration as cfg
from agent import web
from agent.contracts import ROOT


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state_patch = patch.object(cfg, 'STATE', Path(cls.temp.name))
        cls.state_patch.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.state_patch.stop()
        cls.temp.cleanup()
        web.SESSIONS.clear()
        web.JOBS.clear()

    def setUp(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call('/api/status')

    def call(self, path, body=None, origin=None, opener=None):
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
            headers['Origin'] = origin or self.base
        request = urllib.request.Request(self.base + path, data=payload, headers=headers)
        try:
            with (opener or self.opener).open(request) as response:
                data = response.read()
                return response.status, json.loads(data) if 'json' in response.headers['Content-Type'] else data, dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc), dict(exc.headers)

    def session(self, example=False):
        body = {'product_id': '3825952452159603168', 'example': example}
        status, data, _ = self.call('/api/session', body)
        self.assertEqual(status, 200)
        return data

    def test_both_pages_and_js_are_served(self):
        for path in ['/', '/settings', '/app.js', '/style.css']:
            status, body, headers = self.call(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn('Content-Security-Policy', headers)

    def test_14_products_and_no_secret_in_config(self):
        self.assertEqual(len(self.call('/api/products')[1]['products']), 14)
        config = self.call('/api/config')[1]
        self.assertNotIn('DEEPSEEK_API_KEY', config)
        self.assertNotIn('key', self.call('/api/status')[1]['model'])

    def test_unknown_session_owner_is_blocked(self):
        session = self.session()
        different = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call('/api/status', opener=different)
        status, _, _ = self.call('/api/session?id=' + session['id'], opener=different)
        self.assertEqual(status, 400)

    def test_cross_origin_request_is_rejected(self):
        status, _, _ = self.call('/api/session', {}, origin='https://other.invalid')
        self.assertEqual(status, 403)

    def test_core_schema_fields_are_protected(self):
        original = cfg.documents()['schemas']['selling-points.schema.json']
        invalid = copy.deepcopy(original)
        del invalid['properties']['selling_points']
        status, _, _ = self.call('/api/config', {'kind': 'schema', 'name': 'selling-points.schema.json', 'value': invalid})
        self.assertEqual(status, 400)

    def test_schema_description_can_be_changed(self):
        original = cfg.documents()['schemas']['selling-points.schema.json']
        modified = copy.deepcopy(original)
        modified['properties']['selling_points']['description'] = '清楚说明商品的主要价值'
        status, _, _ = self.call('/api/config', {'kind': 'schema', 'name': 'selling-points.schema.json', 'value': modified})
        self.assertEqual(status, 200)
        self.assertEqual(cfg.documents()['schemas']['selling-points.schema.json']['properties']['selling_points']['description'], '清楚说明商品的主要价值')
        cfg.save_document('schema', 'selling-points.schema.json', original)

    def test_added_skill_is_saved_and_loaded(self):
        original = cfg.documents()['extra_skills']
        extra = [{'id': 'sharing', 'name': '分享场景', 'enabled': True, 'content': '仅根据已证实的包装形式描述分享便利，不补充健康功效。'}]
        status, _, _ = self.call('/api/config', {'kind': 'extra_skills', 'value': extra})
        self.assertEqual(status, 200)
        self.assertIn('分享场景', web.SellingAgent.load_skill())
        cfg.save_document('extra_skills', '', original)

    def test_upload_text_and_docx(self):
        session = self.session()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>独立小包装</w:t></w:r></w:p></w:body></w:document>')
        files = [{'name': '属性.txt', 'data': base64.b64encode('商品配料：花生。'.encode()).decode()},
                 {'name': '说明.docx', 'data': base64.b64encode(buffer.getvalue()).decode()}]
        status, data, _ = self.call('/api/upload', {'session_id': session['id'], 'files': files})
        self.assertEqual(status, 200)
        self.assertTrue(any(m['text'] == '独立小包装' for m in data['materials']))

    def test_image_upload_is_tiled_and_asset_is_accessible(self):
        session = self.session()
        stream = io.BytesIO()
        Image.new('RGB', (480, 4000), 'white').save(stream, format='PNG')
        status, data, _ = self.call('/api/upload', {'session_id': session['id'], 'files': [
            {'name': '长图.png', 'data': base64.b64encode(stream.getvalue()).decode()}]})
        self.assertEqual(status, 200)
        uploaded = [m for m in data['materials'] if m['id'].startswith('upload-')]
        self.assertEqual(len(uploaded), 3)
        status, body, _ = self.call(uploaded[0]['preview'])
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'\xff\xd8'))

    def test_failed_upload_batch_does_not_partially_commit(self):
        session = self.session()
        before = len(session['materials'])
        files = [{'name': 'one.txt', 'data': base64.b64encode(b'product info').decode()},
                 {'name': 'two.exe', 'data': base64.b64encode(b'not allowed').decode()}]
        self.assertEqual(self.call('/api/upload', {'session_id': session['id'], 'files': files})[0], 400)
        after = self.call('/api/session?id=' + session['id'])[1]
        self.assertEqual(len(after['materials']), before)

    def test_rule_check_and_manual_score(self):
        session = self.session(example=True)
        report = self.call('/api/evaluate', {'session_id': session['id']})[1]
        self.assertEqual(report['checks'][0]['status'], 'passed')
        self.assertTrue(report['example'])
        scores = {d['id']: 2 for d in cfg.documents()['evaluation']['dimensions']}
        status, result, _ = self.call('/api/score', {'session_id': session['id'], 'scores': scores, 'note': '测试评分'})
        self.assertEqual(status, 200)
        self.assertEqual(result['weighted_score'], 2.0)
        self.assertEqual(result['gate_status'], 'unreviewed')
        self.assertEqual(self.call('/api/score', {'session_id': session['id'], 'scores': {}, 'note': ''})[0], 400)

    def test_rubric_change_does_not_invalidate_generation(self):
        original = cfg.documents()['evaluation']
        before = cfg.documents()['revision']
        modified = copy.deepcopy(original)
        modified['dimensions'][0]['weight'] = 35
        cfg.save_document('evaluation', '', modified)
        self.assertEqual(cfg.documents()['revision'], before)
        cfg.save_document('evaluation', '', original)

    def test_key_is_private_file_and_not_returned(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cfg, 'ROOT', Path(directory)), patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            # 环境变量留空也会优先覆盖文件，因此移除而不是设置空值。
            os.environ.pop('DEEPSEEK_API_KEY', None)
            cfg.save_model('local-test-placeholder-not-a-real-key')
            path = Path(directory) / '.env.local'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            public = cfg.public_model_config()
            self.assertTrue(public['configured'])
            self.assertNotIn('placeholder', json.dumps(public))

    def job_result(self, jid):
        for _ in range(100):
            result = self.call('/api/job?id=' + jid)[1]
            if result['status'] != 'running':
                return result
            threading.Event().wait(0.01)
        self.fail('本地脚本任务没有及时完成')

    def test_chat_job_saves_response_and_clears_busy(self):
        class FakeClient:
            def complete(self, messages, **kwargs):
                return {'role': 'assistant', 'content': '这是本地测试的回复，未调用真实模型。'}
        session = self.session()
        with patch.object(web, 'DeepSeekClient', FakeClient):
            status, job, _ = self.call('/api/chat', {'session_id': session['id'], 'query': '仅测试对话接口'})
            self.assertEqual(status, 200)
            completed = self.job_result(job['job_id'])
        self.assertEqual(completed['status'], 'done')
        self.assertFalse(completed['result']['busy'])
        saved = self.call('/api/session?id=' + session['id'])[1]
        self.assertIn('本地测试', saved['messages'][-1]['text'])
        self.assertFalse(saved['busy'])

    def test_accuracy_job_is_separate_and_persists_report(self):
        session = self.session(example=True)
        before = self.call('/api/session?id=' + session['id'])[1]
        report = {'type': 'accuracy', 'counts': {'supported': 1, 'contradicted': 0, 'insufficient': 0}, 'checks': []}
        with patch.object(web, 'accuracy_review', return_value=report), patch.object(web, 'DeepSeekClient', return_value=object()):
            status, job, _ = self.call('/api/evaluate/accuracy', {'session_id': session['id']})
            self.assertEqual(status, 200)
            done = self.job_result(job['job_id'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual(done['result']['type'], 'accuracy')
        after = self.call('/api/session?id=' + session['id'])[1]
        self.assertEqual(before['result'], after['result'])
        self.assertTrue(list((cfg.STATE / 'evaluations').glob('*.json')))

    def test_profile_edit_confirm_and_history_api(self):
        session = self.session(example=True)
        profile = {k: v for k, v in session['knowledge'].items() if k != '_meta'}
        profile['product_info']['name'] = '商家修订的麻花'
        status, edited, _ = self.call('/api/profile', {'session_id': session['id'], 'profile': profile, 'expected_revision': session['profile_revision']})
        self.assertEqual(status, 200)
        self.assertFalse(edited['profile_confirmed'])
        self.assertEqual(len(edited['profile_history']), 2)
        self.assertIsNone(edited['result'])
        self.assertEqual(self.call('/api/profile/confirm', {'session_id': session['id'], 'expected_revision': session['profile_revision']})[0], 400)
        status, confirmed, _ = self.call('/api/profile/confirm', {'session_id': session['id'], 'expected_revision': edited['profile_revision']})
        self.assertEqual(status, 200)
        self.assertTrue(confirmed['profile_confirmed'])

    def test_evaluation_page_records_and_owner_isolation(self):
        self.assertEqual(self.call('/evaluation')[0], 200)
        self.assertEqual(self.call('/evaluation.js')[0], 200)
        session = self.session(example=True)
        scores = {d['id']: 1 for d in cfg.documents()['evaluation']['dimensions']}
        status, report, _ = self.call('/api/score', {'session_id':session['id'],'scores':scores})
        self.assertEqual(status,200)
        listing = self.call('/api/evaluations?include_examples=true')[1]
        self.assertIn(report['id'],[r['id'] for r in listing['records']])
        self.assertNotIn('artifact',listing['records'][0])
        detail = self.call('/api/evaluations?id='+report['id'])[1]
        self.assertEqual(detail['artifact']['result'],session['result'])
        self.assertNotIn('owner',detail)
        other = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call('/api/status',opener=other)
        self.assertEqual(self.call('/api/evaluations?id='+report['id'],opener=other)[0],400)

    def test_trajectory_report_and_lark_export_are_saved(self):
        session = self.session(example=True)
        status, report, _ = self.call('/api/evaluate/trajectory', {'session_id':session['id']})
        self.assertEqual(status,200)
        self.assertEqual(report['type'],'trajectory')
        with patch.object(web,'export_lark',return_value={'url':'https://example.feishu.cn/docx/unit-test','reused':False}) as export:
            status,job,_ = self.call('/api/evaluation/export',{'report_id':report['id']})
            self.assertEqual(status,200)
            self.assertEqual(self.job_result(job['job_id'])['status'],'done')
            again = self.call('/api/evaluation/export',{'report_id':report['id']})[1]
            self.assertTrue(self.job_result(again['job_id'])['result']['reused'])
            self.assertEqual(export.call_count,1)
        saved = self.call('/api/evaluations?id='+report['id'])[1]
        self.assertIn('lark_url',saved)

    def test_skill_draft_job_is_preview_only(self):
        before = cfg.documents()['extra_skills']
        class FakeClient:
            def complete(self, messages, **kwargs):
                return {'role': 'assistant', 'content': json.dumps({'name': '测试策略', 'content': '保留证据和规格边界。'})}
        with patch.object(web, 'DeepSeekClient', FakeClient):
            status, job, _ = self.call('/api/skill/draft', {'request': '生成一份待预览策略'})
            self.assertEqual(status, 200)
            completed = self.job_result(job['job_id'])
        self.assertEqual(completed['result']['name'], '测试策略')
        self.assertEqual(cfg.documents()['extra_skills'], before)

    def test_sse_delivers_text_before_model_finishes(self):
        release = threading.Event()
        class StreamingClient:
            def stream_complete(self, messages, tools=None, vision=False, on_text=None):
                on_text('**第一段**')
                release.wait(3)
                on_text('，第二段。')
                return {'role': 'assistant', 'content': '**第一段**，第二段。'}
        session = self.session()
        with patch.object(web, 'DeepSeekClient', StreamingClient):
            _, job, _ = self.call('/api/chat', {'session_id': session['id'], 'query': '测试流式回复'})
            try:
                with self.opener.open(self.base + '/api/events?id=' + job['job_id']) as response:
                    first = None
                    for line in response:
                        if line.startswith(b'data:'):
                            snapshot = json.loads(line.decode()[6:])
                            if snapshot['text']:
                                first = snapshot
                                break
                    self.assertIsNotNone(first)
                    self.assertEqual(first['status'], 'running')
                    self.assertEqual(first['text'], '**第一段**')
                    release.set()
                    snapshots = [json.loads(line.decode()[6:]) for line in response if line.startswith(b'data:')]
                    self.assertEqual(snapshots[-1]['status'], 'done')
                    self.assertEqual(snapshots[-1]['text'], '**第一段**，第二段。')
            finally:
                release.set()

    def test_existing_session_receives_connected_images(self):
        session = self.session()
        live = web.SESSIONS[session['id']]
        for item in live['agent'].raw['materials']:
            if item['kind'] == 'image':
                item['image_path'] = ''
        restored = self.call('/api/session?id=' + session['id'])[1]
        self.assertTrue(all(m['image_ready'] for m in restored['materials'] if m['kind'] == 'image'))

    def test_favorite_pin_archive_and_restore_persist(self):
        session = self.session()
        sid = session['id']
        status, _, _ = self.call('/api/session/meta', {'session_id': sid, 'favorite': True, 'pinned': True})
        self.assertEqual(status, 200)
        self.assertEqual(self.call('/api/sessions?filter=favorites')[1]['sessions'][0]['id'], sid)
        self.call('/api/session/meta', {'session_id': sid, 'archived': True})
        self.assertNotIn(sid, [s['id'] for s in self.call('/api/sessions')[1]['sessions']])
        self.assertIn(sid, [s['id'] for s in self.call('/api/sessions?filter=archived')[1]['sessions']])
        web.SESSIONS.pop(sid)
        restored = self.call('/api/session?id=' + sid)[1]
        self.assertTrue(restored['favorite'] and restored['pinned'] and restored['archived'])
        self.call('/api/session/meta', {'session_id': sid, 'archived': False})
        self.assertIn(sid, [s['id'] for s in self.call('/api/sessions')[1]['sessions']])

    def test_regeneration_reuses_query_and_preserves_previous_version(self):
        class FakeClient:
            count = 0
            def complete(self, messages, **kwargs):
                FakeClient.count += 1
                return {'role': 'assistant', 'content': '回复' + str(FakeClient.count)}
        session = self.session()
        with patch.object(web, 'DeepSeekClient', FakeClient):
            original = self.call('/api/chat', {'session_id': session['id'], 'query': '请回答这个问题'})[1]
            self.job_result(original['job_id'])
            retry = self.call('/api/regenerate', {'session_id': session['id']})[1]
            self.assertEqual(self.job_result(retry['job_id'])['status'], 'done')
        saved = self.call('/api/session?id=' + session['id'])[1]
        self.assertEqual(len([m for m in saved['messages'] if m['role'] == 'user']), 1)
        self.assertEqual(saved['messages'][-1]['text'], '回复2')
        self.assertEqual(web.SESSIONS[session['id']]['answer_history'][0]['text'], '回复1')

    def test_regeneration_failure_keeps_previous_answer(self):
        class GoodClient:
            def complete(self, messages, **kwargs):
                return {'role': 'assistant', 'content': '上一版可用回复'}
        class BadClient:
            def complete(self, messages, **kwargs):
                raise RuntimeError('测试连接失败')
        session = self.session()
        with patch.object(web, 'DeepSeekClient', GoodClient):
            job = self.call('/api/chat', {'session_id': session['id'], 'query': '问题'})[1]
            self.job_result(job['job_id'])
        with patch.object(web, 'DeepSeekClient', BadClient):
            job = self.call('/api/regenerate', {'session_id': session['id']})[1]
            self.assertEqual(self.job_result(job['job_id'])['status'], 'error')
        self.assertEqual(self.call('/api/session?id=' + session['id'])[1]['messages'][-1]['text'], '上一版可用回复')


if __name__ == '__main__':
    unittest.main()
