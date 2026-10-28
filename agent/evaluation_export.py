"""评测快照导出到本机登录用户的飞书文档，无消息发送或权限修改。"""
from html import escape
import json
import subprocess
from . import configuration as cfg


def report_xml(report):
    def p(value): return '<p>' + escape(str(value)).replace('\n', '<br/>') + '</p>'
    def heading(value): return '<h1>' + escape(value) + '</h1>'
    def table(headers, rows):
        return '<table><thead><tr>' + ''.join('<th>'+escape(str(h))+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+escape(str(c))+'</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table>'
    title = '点睛评测报告 · ' + report.get('session_title', '商品') + ' · ' + report.get('created_at', '')[:10]
    content = ['<title>' + escape(title) + '</title>',
        p('评测类型：' + report['type'] + '；时间：' + report.get('created_at', '') + ('；人工设计示例' if report.get('example') else '')),
        p('Session：' + str(report.get('session_id'))),
        p('档案版本：' + str(report.get('profile_revision')) + '；卖点版本：' + str(report.get('result_revision'))),
        p('评测时模型配置：' + report.get('model', '未记录') + '；Prompt配置版本：' + str(report.get('config_revision')))]
    if report.get('checks'):
        content += [heading('检查结果'), table(['检查', '状态', '说明'], [(c['name'],c['status'],c.get('detail','')) for c in report['checks']])]
    if report.get('scores'):
        content += [heading('评分结果'), p('加权分：'+str(report.get('quality_score',report.get('weighted_score')))+' / '+str(report.get('score_max',2))), p(report.get('decision',''))]
        rubric = report.get('rubric', {})
        content.append(table(['维度','得分','定义'],[(d['name'],report['scores'].get(d['id'],'未评'),d.get('definition','')) for d in rubric.get('dimensions',[])]))
        if report.get('gate_scores'):
            content.append(table(['底线','得分'],[(d['name'],report['gate_scores'].get(d['id'],'未评')) for d in rubric.get('gates',[])]))
    if report.get('note'): content.append(p('评审备注：'+report['note']))
    artifact = report.get('artifact', {})
    profile = artifact.get('profile')
    if profile:
        content += [heading('第一阶段商品档案快照')]
        for key, name in [('product_info','商品信息'),('specifications','规格配料'),('original_selling_points','原有卖点'),('buyer_feedback','买家反馈'),('cautions','待确认项')]:
            value = profile.get(key)
            if value is not None: content.append(p(name+'：'+json.dumps(value,ensure_ascii=False)))
    result = artifact.get('result', {}) or {}
    if result.get('selling_points'):
        content += [heading('第二阶段卖点快照')]
        content += [p(str(point['priority'])+' · '+point['text']) for point in result['selling_points']]
    if report.get('claims'):
        content += [heading('逐项事实判断'), table(['断言','结论','理由与原文'],[(c['text'],c['verdict'],'\n'.join(r.get('reason','')+' '+json.dumps(r.get('evidence',[]),ensure_ascii=False) for r in c.get('reviews',[]))) for c in report['claims']])]
    if report.get('trace'):
        content += [heading('公开工具轨迹'), table(['序号','工具','状态','工具耗时ms','参数/错误'],[(i+1,t['tool'],'成功' if t.get('ok') else '失败',t.get('duration_ms','未记录'),json.dumps(t.get('arguments',{}),ensure_ascii=False)+' '+t.get('error','')) for i,t in enumerate(report['trace'])])]
    rubric = report.get('rubric')
    if rubric:
        content += [heading('本次使用的评测标准'),p('标准版本：'+rubric.get('version','未记录'))]
        content.append(table(['维度','定义','0分','1分','2分'],[(d['name'],d.get('definition',''),*(d.get('anchors',{}).get(str(i),'未记录') for i in range(3))) for d in rubric.get('gates',[])+rubric.get('dimensions',[])]))
    content += [heading('使用边界'), p('本报告为对应版本的评测快照。评分和工具成功率不代表转化效果；待核项不记为已通过。二期行业Auto Rubrics尚未上线。')]
    return ''.join(content)


def export_lark(report):
    if report.get('lark_url'):
        return {'url': report['lark_url'], 'reused': True}
    xml = report_xml(report)
    try:
        process = subprocess.run(['lark-cli','docs','+create','--content','-','--as','user'], input=xml,
            text=True, capture_output=True, timeout=120, cwd=cfg.ROOT)
    except FileNotFoundError:
        raise RuntimeError('本机未安装lark-cli，可先导出JSON；安装并登录后再导出飞书。') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('飞书创建响应超时，未自动重试以避免重复文档；请先检查飞书最近文档。') from None
    if process.returncode:
        raise RuntimeError('飞书导出未完成，请检查本机lark-cli登录态和文档创建权限；评测记录已保留。')
    try:
        data = json.loads(process.stdout)
        if data.get('ok') is not True: raise ValueError()
        doc = data['data']['document']; url = doc['url']
        if not url.startswith('https://'): raise ValueError()
        return {'url':url,'document_id':doc.get('document_id'),'reused':False}
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('飞书返回格式异常，请先检查最近文档，未自动重试。') from None
