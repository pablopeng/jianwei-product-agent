"""本机双页面Demo服务。HTTP只监听loopback，所有模型调用在服务端。"""
import base64
import copy
from datetime import datetime, timezone
import io
import json
import mimetypes
import os
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile

from . import configuration as cfg
from .contracts import ROOT, revision, validate_result, validate_shape
from .runtime import DeepSeekClient, SellingAgent
from .evaluation import accuracy_review, score_report
from .evaluation_store import save_record, load_records, get_record, public_record, statistics
from .trajectory import review_trajectory, TRAJECTORY_RUBRIC
from .evaluation_export import export_lark
from . import review_batches as rb

WEB = ROOT / 'web'
SESSIONS = {}
JOBS = {}
EXPORT_JOBS = {}
LOCK = threading.RLock()
LABELS = {'list_materials': '查看商品材料', 'search_materials': '检索已有资料',
          'read_material': '读取原始材料', 'get_knowledge': '检查已有信息',
          'record_user_material': '记录补充信息', 'normalize_product': '整理商品信息',
          'load_skill': '加载类目策略', 'generate_selling_points': '生成卖点文案',
          'ask_clarification': '等待你补充信息'}
LABELS.update({'inspect_product': '交叉核查原文与商品图片', 'read_materials': '批量读取商品资料',
    'revise_product_profile': '修改商品档案', 'confirm_product_profile': '确认商品档案'})


def sync_sample_images(session):
    if session.get('busy'):
        return
    agent = session['agent']
    path = ROOT / 'data' / (agent.raw['product_id'] + '.input.json')
    if not path.is_file():
        return
    canonical = json.loads(path.read_text())
    images = {m['id']: m for m in canonical['materials'] if m['kind'] == 'image'}
    updated = copy.deepcopy(agent.raw)
    existing = {m['id'] for m in updated['materials']}
    updated['materials'] = [images.get(m['id'], m) if m['kind'] == 'image' else m for m in updated['materials']]
    updated['materials'].extend(m for mid, m in images.items() if mid not in existing)
    if updated != agent.raw:
        agent.raw = updated
        agent.messages.append({'role': 'system', 'content': '样例商品图片已补齐，原始材料版本已更新。需要时重新核查或整理。'})
        persist(session)


def now():
    return datetime.now(timezone.utc).isoformat()


def manifest():
    return json.loads((ROOT / 'data/manifest.json').read_text())['products']


def persist(session):
    cfg.atomic_write(cfg.STATE / 'sessions' / (session['id'] + '.json'), json.dumps({
        'id': session['id'], 'owner': session['owner'], 'title': session['title'],
        'updated_at': session['updated_at'], 'ui_messages': session['ui_messages'],
        'example': session['example'], 'agent': session['agent'].snapshot(),
        'pinned': session.get('pinned', False), 'favorite': session.get('favorite', False),
        'archived': session.get('archived', False), 'last_turn': session.get('last_turn'),
        'answer_history': session.get('answer_history', [])}, ensure_ascii=False))


def get_session(sid, owner):
    if not isinstance(sid, str) or len(sid) != 32 or any(c not in '0123456789abcdef' for c in sid):
        raise ValueError('会话不存在')
    with LOCK:
        if sid not in SESSIONS:
            path = cfg.STATE / 'sessions' / (sid + '.json')
            if not path.exists():
                raise ValueError('会话不存在')
            state = json.loads(path.read_text())
            state['agent'] = SellingAgent.restore(state['agent'], None)
            state['busy'] = False
            SESSIONS[sid] = state
        result = SESSIONS[sid]
        if result['owner'] != owner:
            raise ValueError('会话不存在')
        sync_sample_images(result)
        return result


def view_session(session):
    agent = session['agent']
    return {'id': session['id'], 'title': session['title'], 'messages': session['ui_messages'],
        'busy': session.get('busy', False), 'example': session['example'],
        'pinned': session.get('pinned', False), 'favorite': session.get('favorite', False),
        'archived': session.get('archived', False),
        'job_id': session.get('job_id'),
        'knowledge': agent.knowledge, 'result': agent.result,
        'profile_revision': revision(agent.knowledge) if agent.knowledge else None,
        'profile_confirmed': agent.profile_confirmed(), 'profile_history': agent.profile_history,
        'profile_stale': bool(agent.knowledge and (agent.knowledge.get('_meta', {}).get('material_revision') != revision(agent.raw) or agent.config_revision != cfg.documents()['revision'])),
        'stale': agent.is_stale(),
        'materials': [{'id': m['id'], 'label': m['label'], 'kind': m['kind'],
            'text': m['text'], 'origin': m['origin'], 'locator': m['locator'],
            'image_ready': bool(m['image_path']),
            'preview': '/api/asset?' + urllib.parse.urlencode({'session': session['id'], 'material': m['id']})
                if m['image_path'] else ''} for m in agent.raw['materials']]}


def new_session(owner, product_id=None, example=False):
    if product_id:
        product = next((p for p in manifest() if p['product_id'] == product_id), None)
        if not product:
            raise ValueError('示例商品不存在')
        raw = json.loads((ROOT / product['input_file']).read_text())
    else:
        raw = {'schema_version': '1.0', 'product_id': 'upload-' + uuid.uuid4().hex,
            'display_name': '新商品', 'materials': [{'id': 'initial', 'kind': 'merchant_supplement',
            'label': '待添加商品资料', 'locator': 'conversation',
            'text': '尚未提供商品事实。请根据用户意图咨询或请用户上传资料，不凭空生成商品卖点。',
            'origin': 'unknown', 'image_path': '', 'image_token': ''}]}
    agent = SellingAgent(raw, None)
    session = {'id': uuid.uuid4().hex, 'owner': owner, 'title': raw['display_name'],
               'updated_at': now(), 'ui_messages': [], 'busy': False, 'example': False, 'agent': agent}
    if example:
        if product_id != '3825952452159603168':
            raise ValueError('当前人工输出示例为蜂蜜花生麻花')
        agent.knowledge = json.loads((ROOT / 'examples/mahua.profile.json').read_text())
        agent.result = json.loads((ROOT / 'examples/mahua.points.json').read_text())
        session['example'] = True
        session['ui_messages'] = [{'role': 'assistant', 'text':
            '这是基于真实商品资料人工设计的输出示例，用于预览结果和依据；尚未调用模型。配置DeepSeek后，可以继续对话或重新生成。'}]
    SESSIONS[session['id']] = session
    persist(session)
    return session


def start_job(owner, work):
    jid = uuid.uuid4().hex
    job = {'id': jid, 'owner': owner, 'status': 'running', 'events': ['开始处理'], 'text': '', 'created_at': now()}
    with LOCK:
        JOBS[jid] = job
        # 仅保留最近100个已完成任务，运行中的任务不会被删除。
        for key in list(JOBS)[:-100]:
            if JOBS[key]['status'] != 'running':
                del JOBS[key]

    def run():
        try:
            def notify(text):
                job['events'].append(text)
            def stream_text(text, reset=False):
                job['text'] = text if reset else job['text'] + text
            notify.text = stream_text
            job['result'] = work(notify)
            job['status'] = 'done'
        except Exception as exc:
            job['error'] = str(exc)[:1200]
            job['status'] = 'error'
    threading.Thread(target=run, daemon=True).start()
    return {'job_id': jid}


def launch_chat(session, query, owner, regenerate=False):
    client = DeepSeekClient()
    previous_answer = None
    if regenerate:
        if not session['ui_messages'] or session['ui_messages'][-1]['role'] not in ['assistant', 'error']:
            raise ValueError('只有最近一条回复可以重新生成')
        previous_answer = copy.deepcopy(session['ui_messages'][-1])
        checkpoint = session.get('last_turn')
        if checkpoint:
            query = checkpoint['query']
            current_history = copy.deepcopy(session['agent'].profile_history)
            session['agent'] = SellingAgent.restore(copy.deepcopy(checkpoint['agent']), client)
            session['agent'].profile_history = current_history
        else:
            user = next((m for m in reversed(session['ui_messages']) if m['role'] == 'user'), None)
            if not user:
                raise ValueError('该演示没有用户问题，请先发起一次对话')
            query = user['text']
            agent = session['agent']
            index = next((i for i in range(len(agent.messages) - 1, -1, -1) if agent.messages[i]['role'] == 'user'), len(agent.messages))
            agent.messages = agent.messages[:index]
            session['last_turn'] = {'query': query, 'agent': copy.deepcopy(agent.snapshot())}
        session.setdefault('answer_history', []).append(previous_answer)
        session['ui_messages'].pop()
    else:
        session['last_turn'] = {'query': query, 'agent': copy.deepcopy(session['agent'].snapshot())}
        session['ui_messages'].append({'role': 'user', 'text': query})
    session['busy'] = True
    session['updated_at'] = now()
    agent = session['agent']
    agent.client = client
    persist(session)
    def chat(event):
        trace_start = len(agent.trace)
        try:
            agent.event_callback = lambda tool: event(LABELS.get(tool, tool))
            agent.text_callback = event.text
            result = agent.turn(query)
            session['example'] = False
            session['ui_messages'].append({'role': 'assistant', 'text': result['message'],
                'process': [LABELS.get(item['tool'], item['tool']) for item in agent.trace[trace_start:]]})
            session['busy'] = False
            return view_session(session)
        except Exception as exc:
            if regenerate and previous_answer:
                session['ui_messages'].append({'role': 'notice', 'text': '重新生成未完成，已保留上一版回复。'})
                session['ui_messages'].append(previous_answer)
            else:
                session['ui_messages'].append({'role': 'error', 'text': '本次处理未完成，请重试。已上传资料仍然保留。'})
            raise
        finally:
            agent.event_callback = agent.text_callback = None
            session['busy'] = False
            session['updated_at'] = now()
            persist(session)
    job = start_job(owner, chat)
    session['job_id'] = job['job_id']
    return job


def ingest_uploads(session, files):
    from PIL import Image, ImageOps
    if not isinstance(files, list) or not 1 <= len(files) <= 12:
        raise ValueError('一次可上传1—12个附件')
    folder = cfg.STATE / 'uploads' / session['id']
    folder.mkdir(parents=True, exist_ok=True)
    staged, written = [], []
    try:
        for item in files:
            name = Path(str(item.get('name', '附件')).replace('\\', '/')).name[:160]
            try:
                payload = base64.b64decode(item['data'], validate=True)
            except Exception:
                raise ValueError('附件编码无效') from None
            if not payload or len(payload) > 12 * 1024 * 1024:
                raise ValueError(name + '为空或超过12MB')
            suffix = Path(name).suffix.lower()
            sid = 'upload-' + uuid.uuid4().hex
            common = {'id': sid, 'label': name, 'locator': 'upload/' + name,
                      'origin': 'merchant', 'image_token': '', 'image_path': ''}
            if suffix in ['.jpg', '.jpeg', '.png', '.webp']:
                with Image.open(io.BytesIO(payload)) as opened:
                    if opened.width * opened.height > 40_000_000:
                        raise ValueError('图片像素过大，请压缩后上传')
                    image = ImageOps.exif_transpose(opened).convert('RGB')
                    if image.width > 1600:
                        image = image.resize((1600, round(image.height * 1600 / image.width)))
                    for index, y in enumerate(range(0, image.height, 1800)):
                        path = folder / (sid + '-' + str(index) + '.jpg')
                        image.crop((0, y, image.width, min(y + 1800, image.height))).save(path, quality=90)
                        written.append(path)
                        staged.append({**common, 'id': sid + '-' + str(index), 'kind': 'image', 'text': '',
                            'label': name + (f' · 片段{index + 1}' if image.height > 1800 else ''),
                            'locator': common['locator'] + f':y={y}', 'image_path': str(path)})
            else:
                if suffix in ['.txt', '.md', '.csv', '.json']:
                    try:
                        text = payload.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        text = payload.decode('gb18030')
                elif suffix == '.docx':
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        if archive.getinfo('word/document.xml').file_size > 20_000_000:
                            raise ValueError('文档内容过大')
                        root = ET.fromstring(archive.read('word/document.xml'))
                        text = '\n'.join(''.join(p.itertext()) for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
                elif suffix == '.pdf':
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(payload))
                    if len(reader.pages) > 60:
                        raise ValueError('PDF请控制在60页以内')
                    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
                    if len(text.strip()) < 10:
                        raise ValueError('该PDF没有可提取文字，请将相关页面作为图片上传')
                else:
                    raise ValueError('暂不支持此附件格式：' + suffix)
                if not text.strip() or len(text) > 250000:
                    raise ValueError('文字内容为空或超过25万字，请拆分资料')
                staged.append({**common, 'kind': 'detail_text', 'text': text})
        if len(staged) > 70:
            raise ValueError('附件拆分后超过70个片段，请分批上传')
        updated = copy.deepcopy(session['agent'].raw)
        updated['materials'].extend(staged)
        validate_shape(updated, 'input.schema.json')
        # 所有附件成功解析后再提交，避免一半成功一半失败。
        for material in staged:
            session['agent'].add_material(material)
        session['example'] = False
        if session['title'] == '新商品':
            session['title'] = str(files[0]['name'])[:60]
        session['updated_at'] = now()
        session['ui_messages'].append({'role': 'notice', 'text': '已添加 ' + str(len(files)) + ' 个附件。'})
        persist(session)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return view_session(session)


class Handler(BaseHTTPRequestHandler):
    server_version = 'SellingAgent/0.2'

    def log_message(self, format, *args):
        # 不记录请求正文、密钥、用户资料或query string。
        pass

    def prepare(self):
        host = self.headers.get('Host', '')
        cloud = os.environ.get('AGENT_DEPLOYMENT') == 'cloud'
        allowed = ({v.strip() for v in os.environ.get('AGENT_ALLOWED_HOSTS', '').split(',') if v.strip()}
                   if cloud else {'localhost:' + str(self.server.server_port), '127.0.0.1:' + str(self.server.server_port)})
        if host not in allowed:
            raise PermissionError('访问域名不在允许列表中')
        origin = self.headers.get('Origin')
        allowed_origins = {('https://' if cloud else 'http://') + value for value in allowed}
        if origin and origin not in allowed_origins:
            raise PermissionError('请求来源不匹配')
        if cloud:
            expected = os.environ.get('DEMO_ACCESS_PASSWORD', '')
            supplied = self.headers.get('Authorization', '')
            try:
                raw = base64.b64decode(supplied[6:], validate=True).decode('utf-8') if supplied.startswith('Basic ') else ''
                username, password = raw.split(':', 1)
            except (ValueError, UnicodeDecodeError):
                username, password = '', ''
            if not expected or username != 'demo' or not hmac.compare_digest(password.encode(), expected.encode()):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Dianjing Demo", charset="UTF-8"')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return False
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
        except cookies.CookieError:
            pass
        token = jar.get('agent_owner')
        value = token.value if token else ''
        self.owner = value if len(value) == 48 and all(c in '0123456789abcdef' for c in value) else secrets.token_hex(24)
        self.new_cookie = self.owner != value
        return True

    def headers_out(self, code, kind, length=None):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if getattr(self, 'new_cookie', False):
            self.send_header('Set-Cookie', 'agent_owner=' + self.owner + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000' + ('; Secure' if os.environ.get('AGENT_DEPLOYMENT') == 'cloud' else ''))
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def respond(self, value, code=200):
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.headers_out(code, 'application/json; charset=utf-8', len(payload))
        self.wfile.write(payload)

    def do_GET(self):
        try:
            if self.prepare() is False:
                return
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == '/api/status':
                return self.respond({'model': cfg.public_model_config(), 'config_revision': cfg.documents()['revision']})
            if url.path == '/api/products':
                return self.respond({'products': manifest()})
            if url.path == '/api/config':
                return self.respond(cfg.documents())
            if url.path == '/api/sessions':
                sessions = []
                for path in (cfg.STATE / 'sessions').glob('*.json'):
                    data = json.loads(path.read_text())
                    if data['owner'] == self.owner:
                        sessions.append({**{k: data[k] for k in ['id', 'title', 'updated_at']},
                            **{k: data.get(k, False) for k in ['pinned', 'favorite', 'archived']}})
                mode = query.get('filter', ['active'])[0]
                sessions = [s for s in sessions if (True if mode == 'all' else s['archived'] if mode == 'archived' else not s['archived'])
                            and (s['favorite'] if mode == 'favorites' else True)]
                return self.respond({'sessions': sorted(sessions, key=lambda s: (s['pinned'], s['updated_at']), reverse=True)})
            if url.path == '/api/review-batches':
                return self.respond({'batches': rb.list_batches(self.owner)})
            if url.path == '/api/review-batches/candidates':
                candidates = []
                for p in (cfg.STATE / 'sessions').glob('*.json'):
                    v = json.loads(p.read_text())
                    if v['owner'] != self.owner: continue
                    current = get_session(v['id'], self.owner)
                    a = current['agent']
                    ready = bool(a.knowledge and a.result and 'selling_points' in a.result and 'product_info' in a.knowledge and not current.get('busy'))
                    candidates.append({'id': v['id'], 'title': v['title'], 'ready': ready, 'example': v['example'],
                        'updated_at': v['updated_at'], 'point_count': len((a.result or {}).get('selling_points', [])),
                        'stale': a.is_stale(), 'reason': '' if ready else '需先完成档案与卖点生成'})
                return self.respond({'candidates': sorted(candidates, key=lambda c:c['updated_at'], reverse=True)})
            if url.path == '/api/evaluations':
                rid = query.get('id', [''])[0]
                if rid:
                    return self.respond(public_record(get_record(self.owner, rid), detail=True))
                records = load_records(self.owner)
                return self.respond({'records': [public_record(r) for r in records],
                    'statistics': statistics(records, query.get('include_examples', ['false'])[0] == 'true'),
                    'trajectory_rubric': TRAJECTORY_RUBRIC})
            if url.path == '/api/session':
                return self.respond(view_session(get_session(query.get('id', [''])[0], self.owner)))
            if url.path == '/api/job':
                job = JOBS.get(query.get('id', [''])[0])
                if not job or job['owner'] != self.owner:
                    raise ValueError('任务不存在')
                return self.respond({k: v for k, v in job.items() if k != 'owner'})
            if url.path == '/api/events':
                job = JOBS.get(query.get('id', [''])[0])
                if not job or job['owner'] != self.owner:
                    raise ValueError('任务不存在')
                self.headers_out(200, 'text/event-stream; charset=utf-8')
                previous = None
                while True:
                    public = {k: job[k] for k in ['status', 'events', 'text']}
                    if job.get('error'):
                        public['error'] = job['error']
                    encoded = json.dumps(public, ensure_ascii=False)
                    if encoded != previous:
                        try:
                            self.wfile.write(('data: ' + encoded + '\n\n').encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        previous = encoded
                    if public['status'] != 'running':
                        return
                    time.sleep(0.12)
            if url.path == '/api/asset':
                session = get_session(query.get('session', [''])[0], self.owner)
                material = next((m for m in session['agent'].raw['materials'] if m['id'] == query.get('material', [''])[0]), None)
                if not material or not material['image_path']:
                    raise ValueError('图片不存在')
                path = Path(material['image_path'])
                path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
                if not any(base.resolve() in path.parents for base in [ROOT / 'data/assets', cfg.STATE / 'uploads']):
                    raise PermissionError('图片路径不可访问')
                payload = path.read_bytes()
                self.headers_out(200, mimetypes.guess_type(str(path))[0] or 'image/jpeg', len(payload))
                return self.wfile.write(payload)
            static = {'/': 'index.html', '/settings': 'index.html', '/evaluation': 'index.html', '/app.js': 'app.js', '/evaluation.js': 'evaluation.js', '/style.css': 'style.css',
                      '/vendor/marked.umd.js': 'vendor/marked.umd.js', '/vendor/purify.min.js': 'vendor/purify.min.js'}
            if url.path not in static:
                return self.respond({'error': '页面不存在'}, 404)
            path = WEB / static[url.path]
            payload = path.read_bytes()
            self.headers_out(200, (mimetypes.guess_type(str(path))[0] or 'text/plain') + '; charset=utf-8', len(payload))
            self.wfile.write(payload)
        except PermissionError as exc:
            self.respond({'error': str(exc)}, 403)
        except (ValueError, FileNotFoundError) as exc:
            self.respond({'error': str(exc)}, 400)
        except Exception:
            self.respond({'error': '服务读取失败，请重试'}, 500)

    def do_POST(self):
        try:
            if self.prepare() is False:
                return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 30 * 1024 * 1024:
                raise ValueError('请求为空或超过30MB')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('请求必须是JSON对象')
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/session':
                return self.respond(view_session(new_session(self.owner, data.get('product_id'), data.get('example', False))))
            if path == '/api/config':
                if any(s.get('busy') for s in SESSIONS.values()):
                    raise ValueError('请等待正在执行的会话结束后修改规则')
                result = cfg.save_document(data['kind'], data.get('name', ''), data['value'])
                return self.respond({'ok': True, 'revision': result['revision']})
            if path == '/api/model':
                cfg.save_model(data.get('key', ''), data.get('model', 'deepseek-flash'), data.get('vision_model', 'deepseek-flash'))
                return self.respond(cfg.public_model_config())
            if path == '/api/model/test':
                def probe(event):
                    event('正在请求DeepSeek验证连接')
                    result = DeepSeekClient().complete([{'role': 'user', 'content': '请只回复：连接正常'}])
                    return {'message': result.get('content', '模型已响应')}
                return self.respond(start_job(self.owner, probe))
            if path == '/api/skill/draft':
                request = data.get('request', '')
                if not isinstance(request, str) or not request.strip() or len(request) > 6000:
                    raise ValueError('请填写Skill要求，最多6000字')
                def draft(event):
                    event('正在起草Skill，保存前可修改')
                    response = DeepSeekClient().complete([
                        {'role': 'system', 'content': '为商品卖点Agent起草可叠加的类目Skill。输出JSON，字段name和content。保留证据/SKU边界，不改变Schema，不把用户要求当商品事实。只生成供用户审阅的草稿。'},
                        {'role': 'user', 'content': request}], structured=True)
                    result = json.loads(response.get('content', ''))
                    if not isinstance(result.get('name'), str) or not isinstance(result.get('content'), str):
                        raise ValueError('模型未返回有效Skill，请重试')
                    return result
                return self.respond(start_job(self.owner, draft))
            if path in ['/api/upload', '/api/chat', '/api/regenerate', '/api/session/meta', '/api/evaluate', '/api/evaluate/accuracy', '/api/score', '/api/profile', '/api/profile/confirm', '/api/evaluate/trajectory']:
                session = get_session(data.get('session_id', ''), self.owner)
                if session.get('busy'):
                    raise ValueError('当前会话正在处理，请等待本轮完成')
            if path == '/api/upload':
                return self.respond(ingest_uploads(session, data.get('files', [])))
            if path == '/api/session/meta':
                for key in ['pinned', 'favorite', 'archived']:
                    if key in data:
                        if not isinstance(data[key], bool):
                            raise ValueError('状态值必须是布尔值')
                        session[key] = data[key]
                persist(session)
                return self.respond(view_session(session))
            if path == '/api/profile':
                session['agent'].edit_profile(data.get('profile'), data.get('expected_revision'))
                session['example'] = False
                session['last_turn'] = None
                session['updated_at'] = now()
                persist(session)
                return self.respond(view_session(session))
            if path == '/api/profile/confirm':
                session['agent'].confirm_profile(data.get('expected_revision'))
                session['updated_at'] = now()
                persist(session)
                return self.respond(view_session(session))
            if path == '/api/regenerate':
                return self.respond(launch_chat(session, '', self.owner, regenerate=True))
            if path == '/api/chat':
                query = data.get('query', '').strip()
                if not query or len(query) > 20000:
                    raise ValueError('请输入1—20000字的要求')
                return self.respond(launch_chat(session, query, self.owner))
            if path == '/api/review-batches/create':
                ids = data.get('session_ids')
                if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
                    raise ValueError('请选择1—100个不同Session')
                batch = rb.create_batch(self.owner, [get_session(sid, self.owner) for sid in ids], data.get('name'))
                return self.respond(rb.summary(batch))
            if path in ['/api/review-batches/export', '/api/review-batches/import']:
                batch = rb.load_batch(self.owner, data.get('batch_id'))
                key = 'batch:' + batch['id']
                with LOCK:
                    pending = EXPORT_JOBS.get(key)
                    if pending and JOBS.get(pending, {}).get('status') == 'running':
                        raise ValueError('本批次正在处理，请等待完成')
                    def work(event):
                        try:
                            return rb.export_batch(batch, event) if path.endswith('/export') else rb.import_batch(batch, event)
                        except Exception as exc:
                            batch['last_error'] = str(exc)[:1000]
                            rb.persist(batch)
                            raise
                    job = start_job(self.owner, work)
                    EXPORT_JOBS[key] = job['job_id']
                return self.respond(job)
            if path == '/api/evaluation/export':
                record = get_record(self.owner, data.get('report_id'))
                if record.get('lark_url'):
                    return self.respond(start_job(self.owner, lambda event: {'url': record['lark_url'], 'reused': True}))
                with LOCK:
                    existing = EXPORT_JOBS.get(record['id'])
                    if existing and JOBS.get(existing, {}).get('status') == 'running':
                        return self.respond({'job_id': existing})
                    def export(event):
                        event('正在创建飞书评测文档')
                        result = export_lark(record)
                        record['lark_url'] = result['url']
                        folder = record.pop('storage')
                        cfg.atomic_write(cfg.STATE / folder / (record['id'] + '.json'), json.dumps(record, ensure_ascii=False, indent=2))
                        return result
                    job = start_job(self.owner, export)
                    EXPORT_JOBS[record['id']] = job['job_id']
                return self.respond(job)
            if path == '/api/evaluate/trajectory':
                report = review_trajectory(session['agent'])
                return self.respond(save_record(self.owner, session, report, 'trajectory'))
            if path == '/api/evaluate/accuracy':
                if view_session(session)['stale'] or view_session(session)['profile_stale'] or session.get('busy'):
                    raise ValueError('请等待生成完成，并使用当前版本结果')
                snapshot = copy.deepcopy(session['agent'].snapshot())
                review_session = {**session, 'ui_messages': copy.deepcopy(session['ui_messages'])}
                def review(event):
                    agent = SellingAgent.restore(snapshot, DeepSeekClient())
                    report = accuracy_review(agent, event)
                    return save_record(self.owner, review_session, report, 'accuracy', agent=agent)
                return self.respond(start_job(self.owner, review))
            if path == '/api/evaluate':
                view = view_session(session)
                agent = session['agent']
                if not agent.result or not agent.knowledge:
                    raise ValueError('请先选择有结果的会话或运行生成')
                checks = []
                try:
                    if view['stale']:
                        raise ValueError('资料或配置已更新，当前结果已过期')
                    validate_result(agent.result, agent.knowledge)
                    checks.append({'name': '结构、引用、版本与SKU范围', 'status': 'passed'})
                except Exception as exc:
                    checks.append({'name': '结构、引用、版本与SKU范围', 'status': 'failed', 'detail': str(exc)[:1200]})
                report = {'checks': checks,
                    'note': '规则检查不证明语义正确、商品声称成立或文案有转化效果。',
                    'dimensions': cfg.documents()['evaluation']['dimensions']}
                return self.respond(save_record(self.owner, session, report, 'structure'))
            if path == '/api/score':
                view = view_session(session)
                scope = data.get('scope', 'quality')
                if scope not in ['quality', 'trajectory']:
                    raise ValueError('未知评分对象')
                if scope == 'quality' and (not session['agent'].result or view['stale']):
                    raise ValueError('请先生成有效的当前版本文案')
                if scope == 'trajectory' and session.get('busy'):
                    raise ValueError('请等待当前会话处理完成')
                rubric = TRAJECTORY_RUBRIC if scope == 'trajectory' else cfg.documents()['evaluation']
                scored = score_report(data.get('scores', {}), data.get('gate_scores', {}), rubric)
                trajectory = review_trajectory(session['agent']) if scope == 'trajectory' else {}
                if scope == 'trajectory':
                    scored['gate_status'] = 'not_applicable'
                    scored['decision'] = '轨迹人工评分，需结合规则检查与实际任务完成情况判断'
                report = {**trajectory, 'rubric': rubric, 'note': str(data.get('note', ''))[:10000], **scored}
                return self.respond(save_record(self.owner, session, report, scope + '_score'))
            return self.respond({'error': '接口不存在'}, 404)
        except PermissionError as exc:
            self.respond({'error': str(exc)}, 403)
        except (ValueError, KeyError, TypeError) as exc:
            self.respond({'error': str(exc)[:1200]}, 400)
        except RuntimeError as exc:
            self.respond({'error': str(exc)[:1200]}, 502)
        except Exception:
            self.respond({'error': '处理失败，请检查输入或稍后重试'}, 500)


def serve(port=8765, host='127.0.0.1'):
    if os.environ.get('AGENT_DEPLOYMENT') == 'cloud':
        if len(os.environ.get('DEMO_ACCESS_PASSWORD', '')) < 16 or not os.environ.get('AGENT_ALLOWED_HOSTS'):
            raise ValueError('云端启动需要至少16位DEMO_ACCESS_PASSWORD和明确的AGENT_ALLOWED_HOSTS')
    server = ThreadingHTTPServer((host, port), Handler)
    print('商品卖点Agent已启动：http://' + host + ':' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
