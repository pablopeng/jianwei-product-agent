"""按使用者保存版本化评测记录，统计同一快照的最新评审。"""
import copy
from datetime import datetime, timezone
import json
import uuid
from . import configuration as cfg
from .contracts import revision


def save_record(owner, session, report, kind, agent=None):
    agent = agent or session['agent']
    report = {**copy.deepcopy(report), 'id': uuid.uuid4().hex, 'type': kind, 'owner': owner,
        'session_id': session['id'], 'session_title': session['title'],
        'created_at': datetime.now(timezone.utc).isoformat(), 'product_id': agent.raw['product_id'],
        'example': session['example'], 'profile_revision': revision(agent.knowledge) if agent.knowledge else None,
        'result_revision': revision(agent.result) if agent.result else None,
        'config_revision': agent.config_revision, 'model': getattr(agent.client, 'model', None) or cfg.public_model_config()['model'],
        'model_note': '评测调用/当时服务配置；旧记录的实际生成模型未单独记录',
        'artifact': {'profile': copy.deepcopy(agent.knowledge), 'result': copy.deepcopy(agent.result),
                     'messages': copy.deepcopy(session['ui_messages']),
                     'tool_observations': [copy.deepcopy(m) for m in agent.messages if m.get('role') == 'tool']}}
    folder = 'scores' if kind in ['quality_score', 'trajectory_score'] else 'evaluations'
    cfg.atomic_write(cfg.STATE / folder / (report['id'] + '.json'), json.dumps(report, ensure_ascii=False, indent=2))
    return {k: v for k, v in report.items() if k != 'owner'}


def load_records(owner):
    owned = set()
    for p in (cfg.STATE / 'sessions').glob('*.json'):
        value = json.loads(p.read_text())
        if value.get('owner') == owner:
            owned.add(value['id'])
    rows = []
    for folder in ['scores', 'evaluations']:
        for p in (cfg.STATE / folder).glob('*.json'):
            row = json.loads(p.read_text())
            if row.get('owner') == owner or ('owner' not in row and row.get('session_id') in owned):
                rows.append({**row, 'id': p.stem, 'type': row.get('type', 'quality_score'), 'storage': folder})
    return sorted(rows, key=lambda r: r.get('created_at', ''), reverse=True)


def get_record(owner, rid):
    if not isinstance(rid, str) or len(rid) != 32 or any(c not in '0123456789abcdef' for c in rid):
        raise ValueError('评测记录不存在')
    row = next((r for r in load_records(owner) if r['id'] == rid), None)
    if not row:
        raise ValueError('评测记录不存在')
    return row


def public_record(row, detail=False):
    hidden = ['owner', 'storage'] + ([] if detail else ['artifact', 'claims', 'trace', 'rubric'])
    return {k: v for k, v in row.items() if k not in hidden}


ADVICE = {
 'coverage': ('一阶段摘要 / 二阶段候选盘点', '先列出档案内独立且有依据的购买理由，对照生成结果查漏、合并同义点。'),
 'value': ('二阶段选点策略', '优先选择与用户使用、体验和明确诉求相关的理由，减少无关背景和泛好评。'),
 'distinct': ('二阶段排序', '把最能代表当前商品的具体特点排前，减少可替换商品名的套话。'),
 'hook': ('二阶段标题表达', '让标题概括一个具体好处或体验，兼顾一眼理解和记忆点，避免只有品质好、很美味。'),
 'vivid': ('二阶段正文表达 / 终稿复核', '正文在标题之外补充理由、证据细节或使用体验；不为画面感新增商品性状和承诺。'),
 'clarity': ('二阶段终稿复核', '一条聚焦一个理由，限定贴近承诺，删重复解释和内部分析用语。'),
 'intent': ('主 Agent', '先判断当前意图是核查、建档、修改还是生成，只执行需要的部分。'),
 'tools': ('主 Agent / 工具说明', '完整核查用批量材料或核查工具，小问题定向检索，并保留来源范围。'),
 'interaction': ('主 Agent / 确认机制', '建档或事实修改后停在确认环节，已确认且资料未变时复用档案。'),
 'efficiency': ('主 Agent / 工具预算', '复用已读证据和有效档案；避免同轮相同参数重复调用。'),
 'recovery': ('主 Agent / 错误处理', '工具失败时先修复或明确停止，禁止在公开回复中宣称未完成的结果已成功。')}


def statistics(rows, include_examples=False):
    selected = [r for r in rows if include_examples or not r.get('example')]
    latest = {}
    for r in sorted(selected, key=lambda r: r.get('created_at', ''), reverse=True):
        key = (r['type'], r.get('session_id'), r.get('profile_revision'), r.get('result_revision'), revision(r.get('rubric', {})))
        if key not in latest: latest[key] = r
    chosen = list(latest.values())
    groups = {}
    for r in chosen:
        if r['type'] not in ['quality_score', 'trajectory_score'] or not r.get('rubric'):
            continue
        rubric = r['rubric']; key = r['type'] + ':' + revision(rubric)
        g = groups.setdefault(key, {'type': r['type'], 'rubric_version': rubric.get('version', 'legacy'),
            'rubric_revision': revision(rubric), 'score_max': r.get('score_max', rubric.get('score_range', [1, 5])[1]), 'samples': 0, 'dimensions': {}, 'gate_statuses': {}})
        g['samples'] += 1
        status = r.get('gate_status', 'unreviewed'); g['gate_statuses'][status] = g['gate_statuses'].get(status, 0) + 1
        for d in rubric['dimensions']:
            score = r.get('scores', {}).get(d['id'])
            if type(score) not in [int, float]: continue
            item = g['dimensions'].setdefault(d['id'], {'id': d['id'], 'name': d['name'], 'values': []})
            item['values'].append(score)
    advice = []
    for g in groups.values():
        dims = []
        for d in g['dimensions'].values():
            values = d.pop('values');d['n'] = len(values);d['mean'] = round(sum(values)/len(values), 2)
            d['distribution'] = {str(i): values.count(i) for i in range(int(g['score_max']) + 1)};dims.append(d)
            if d['mean'] <= 1.2 and d['id'] in ADVICE:
                target, text = ADVICE[d['id']]
                advice.append({'dimension': d['name'], 'mean': d['mean'], 'n': d['n'], 'target': target,
                    'suggestion': text, 'rubric_version': g['rubric_version'], 'kind': '规则建议，未验证效果'})
        g['dimensions'] = dims
    facts = {'supported': 0, 'contradicted': 0, 'insufficient': 0}
    for r in chosen:
        if r['type'] == 'accuracy':
            for k in facts: facts[k] += r.get('counts', {}).get(k, 0)
    return {'total_records': len(rows), 'selected_records': len(selected), 'latest_records': len(chosen),
        'rated_results': sum(r['type']=='quality_score' for r in chosen),
        'sessions': len({r.get('session_id') for r in chosen}), 'groups': list(groups.values()),
        'accuracy_claims': facts, 'suggestions': sorted(advice, key=lambda a: a['mean']),
        'note': '默认排除人工示例；同一会话、档案/卖点版本及标准取最新记录。不同标准版本分组，事实统计按断言计数；不是独立商品数或转化率。'}
