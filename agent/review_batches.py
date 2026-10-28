"""冻结一批商品产物→飞书人工评分表→回收评分；只导出已有结果，不自动生成或确认。"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid
from types import SimpleNamespace
from . import configuration as cfg
from .contracts import revision
from .compact import public
from .evaluation import score_report
from .evaluation_store import save_record

SHEET = '人工评审'
META = ['样本ID','商品ID','SessionID','档案版本','卖点版本','批次ID','标准版本']

def now(): return datetime.now(timezone.utc).isoformat()
def columns(rubric):
    out=['原始商品信息','一阶段商品依据','二阶段卖点']
    for d in rubric['gates']+rubric['dimensions']:
        out += [d['name']+'（0/1/2）',d['name']+'备注']
    return out+['总体备注','评审人']+META

def col(index):
    s='';index+=1
    while index:index,n=divmod(index-1,26);s=chr(65+n)+s
    return s

def load_batch(owner, bid):
    if not isinstance(bid,str) or len(bid)!=32 or any(c not in '0123456789abcdef' for c in bid):raise ValueError('批次不存在')
    path=cfg.STATE/'review-batches'/(bid+'.json')
    if not path.exists():raise ValueError('批次不存在')
    batch=json.loads(path.read_text())
    if batch['owner']!=owner:raise ValueError('批次不存在')
    return batch

def persist(batch):cfg.atomic_write(cfg.STATE/'review-batches'/(batch['id']+'.json'),json.dumps(batch,ensure_ascii=False,indent=2))
def list_batches(owner):
    return sorted([summary(b) for p in (cfg.STATE/'review-batches').glob('*.json')
                   for b in [json.loads(p.read_text())] if b['owner']==owner],key=lambda b:b['created_at'],reverse=True)
def summary(batch):
    return {k:batch.get(k) for k in ['id','name','created_at','sample_count','sheet_url','export_status','imported_at','progress','analysis','last_error']}

def create_batch(owner,sessions,name):
    if not 1<=len(sessions)<=100 or len({s['id'] for s in sessions})!=len(sessions):raise ValueError('每批选择1—100个不同Session')
    rubric=copy.deepcopy(cfg.documents()['evaluation']);samples=[]
    for s in sessions:
        a=s['agent']
        if s.get('busy') or not a.knowledge or not a.result or 'selling_points' not in a.result or 'product_info' not in a.knowledge:
            raise ValueError('所选Session需已有一阶段档案和二阶段卖点，且当前未运行')
        samples.append({'sample_id':uuid.uuid4().hex[:16],'session_id':s['id'],'title':s['title'],'product_id':a.raw['product_id'],
            'raw':copy.deepcopy(a.raw),'profile':copy.deepcopy(a.knowledge),'result':copy.deepcopy(a.result),
            'profile_revision':revision(a.knowledge),'result_revision':revision(a.result),'config_revision':a.config_revision,
            'example':s['example'],'messages':copy.deepcopy(s['ui_messages'])})
    batch={'id':uuid.uuid4().hex,'owner':owner,'name':str(name or '商品卖点批量评审')[:100],
        'created_at':now(),'rubric':rubric,'sample_count':len(samples),'samples':samples,'ratings':{},'export_status':'not_started'}
    persist(batch);return batch

def source_book_url(raw):
    if not raw['product_id'].startswith('upload-'):
        return json.loads((cfg.ROOT/'data/manifest.json').read_text()).get('source_url','')
    return ''

def profile_text(p):
    labels={'product_info':'商品信息','specifications':'规格配料','original_selling_points':'原有卖点','buyer_feedback':'买家反馈','cautions':'待确认项'}
    return '\n\n'.join(labels[k]+'\n'+json.dumps(v,ensure_ascii=False) for k,v in public(p).items())

def build_workbook(batch):
    rubric=batch['rubric'];heads=columns(rubric);review=[];raw_rows=[];image_tasks=[]
    for s in batch['samples']:
        raw=s['raw'];source_url=source_book_url(raw);texts=[]
        for m in raw['materials']:
            if m['kind']!='image':texts.append(f"〔{m['id']}〕{m['label']}\n{m['text']}")
            text=m['text'];chunks=[text[i:i+18000] for i in range(0,len(text),18000)] or ['']
            for i,chunk in enumerate(chunks):
                raw_rows.append([s['sample_id'],m['id'],m['kind'],m['label'],chunk,source_url,m['locator'],''])
                if m['kind']=='image' and not source_url and m.get('image_path'):
                    image_tasks.append({'row':len(raw_rows)+1,'path':m['image_path'],'sample_id':s['sample_id']})
        original=raw['display_name']+'\n'+('\n\n'.join(texts))
        if len(original)>14000:original=original[:14000]+'\n【评审主表为节选，完整原文请按样本ID查看“原始资料”页；未丢弃原始内容】'
        if source_url:original+='\n商品图与详情图原始表：'+source_url+'；图片定位见原始资料页。'
        points='\n\n'.join(str(p['priority'])+'. '+p['text'] for p in s['result']['selling_points'])
        row=[original,profile_text(s['profile']),points]+['']*(2*(len(rubric['gates'])+len(rubric['dimensions'])))+['','']
        row += [s['sample_id'],s['product_id'],s['session_id'],s['profile_revision'],s['result_revision'],batch['id'],rubric['version']]
        assert len(row)==len(heads)
        review.append(row)
    standards=[]
    for group,ds in [('底线',rubric['gates']),('高标',rubric['dimensions'])]:
        for d in ds:standards.append([group,d['name'],d['definition'],d['anchors']['0'],d['anchors']['1'],d['anchors']['2'],d.get('weight','')])
    standards += [['说明','评分单位','一行是一份商品的整组卖点；空白表示未评，不等于0分。','0：差/明确问题','1：可用/待核','2：好/未发现该类问题',''],
        ['说明','问题归类',rubric.get('classification',''),'','','',''],
        ['说明','资料来源','原始资料页保留材料原文，内置商品图通过原始在线表及定位查看；上传图片嵌入原始资料页。','','','',''],
        ['说明','标准性质',rubric['note'],'','','','']]
    sources=json.loads((cfg.ROOT/'evaluation/public-sources.json').read_text())
    standards += [['参考',x['title'],x['use'],x['url'],'','',''] for x in sources]
    sheets=[{'name':SHEET,'columns':heads,'data':review,'dtypes':{h:'object' for h in heads}},
        {'name':'评分标准','columns':['层级','维度','定义/说明','0分','1分','2分','权重'],'data':standards},
        {'name':'原始资料','columns':['样本ID','材料ID','资料类型','资料名','原文','原始图文表','来源定位','上传图片'],'data':raw_rows,
         'dtypes':{h:'object' for h in ['样本ID','材料ID','资料类型','资料名','原文','原始图文表','来源定位','上传图片']}}]
    styles=[]
    for sh in sheets:
        end=col(len(sh['columns'])-1);n=len(sh['data'])+1
        row={'name':sh['name'],'cell_styles':[{'range':f'A1:{end}{n}','word_wrap':'auto-wrap','vertical_alignment':'top','font_size':11},
            {'range':f'A1:{end}1','font_weight':'bold','font_color':'#ffffff','background_color':'#6654c0'}],
            'row_sizes':[{'range':'1:1','type':'pixel','size':42},{'range':f'2:{n}','type':'pixel','size':180}],
            'col_sizes':[{'range':f'A:{end}','type':'pixel','size':150}]}
        if sh['name']==SHEET:row['col_sizes'] += [{'range':'A:C','type':'pixel','size':350}]
        if sh['name']=='原始资料':row['col_sizes'] += [{'range':'E:E','type':'pixel','size':500}]
        if sh['name']=='评分标准':row['col_sizes'] += [{'range':'C:F','type':'pixel','size':300}]
        styles.append(row)
    return {'sheets':sheets},{'styles':styles},image_tasks

def cli(args,payload=None):
    try:r=subprocess.run(['lark-cli','sheets']+args+['--as','user'],input=json.dumps(payload,ensure_ascii=False) if payload is not None else None,
                         text=True,capture_output=True,timeout=180,cwd=cfg.ROOT)
    except FileNotFoundError:raise RuntimeError('本机未安装lark-cli，请先安装并登录飞书') from None
    except subprocess.TimeoutExpired:raise RuntimeError('飞书请求响应超时；请检查批次状态和飞书最近文档，不自动重复创建') from None
    try:d=json.loads(r.stdout if r.returncode==0 else r.stderr)
    except ValueError:raise RuntimeError('飞书返回格式异常，批次已保留') from None
    if r.returncode or d.get('ok') is not True:raise RuntimeError('飞书表格操作未完成，请检查登录态与表格权限；已保留批次')
    return d['data']

def export_batch(batch,notify=lambda t:None):
    if batch.get('export_status')=='ready':return summary(batch)
    if not batch.get('sheet_token') and batch.get('export_status')=='creating':
        raise RuntimeError('上次建表结果尚不确定，请先检查飞书最近文档，不自动创建第二份')
    if not batch.get('sheet_token'):
        batch['export_status']='creating';persist(batch)
        payload,styles,images=build_workbook(batch)
        # 大数据从stdin传入；样式作为结构化JSON参数传入。
        notify('创建批量人工评审表')
        data=cli(['+workbook-create','--title',batch['name'],'--sheets','-','--styles',json.dumps(styles,ensure_ascii=False)],payload)
        # workbook-info的返回字段在不同CLI版本可能嵌套于spreadsheet。
        book=data.get('spreadsheet',data);token=book.get('spreadsheet_token') or book.get('token')
        if not token:raise RuntimeError('表格已请求创建，但未取得token；请检查飞书最近文档，不自动重复创建')
        batch.update(sheet_token=token,sheet_url=book.get('url') or book.get('spreadsheet_url'),export_status='configuring')
        persist(batch)
    token=batch['sheet_token'];info=cli(['+workbook-info','--spreadsheet-token',token])
    sh=next(s for s in info['sheets'] if s.get('title',s.get('sheet_name'))==SHEET)
    batch['sheet_id']=sh['sheet_id']
    if not batch.get('sheet_url'):batch['sheet_url']=info.get('url') or info.get('spreadsheet_url')
    n=batch['sample_count']+1;nd=len(batch['rubric']['gates'])+len(batch['rubric']['dimensions'])
    ranges=[f'{SHEET}!{col(3+2*i)}2:{col(3+2*i)}{n}' for i in range(nd)]
    notify('设置0/1/2下拉评分')
    cli(['+dropdown-update','--spreadsheet-token',token,'--ranges',json.dumps(ranges,ensure_ascii=False),
         '--options','["0","1","2"]','--colors','["#FDE2E2","#FFF2CC","#D9EAD3"]'])
    payload,_,images=build_workbook(batch)
    for i,task in enumerate(images,1):
        path=Path(task['path']);path=path if path.is_absolute() else cfg.ROOT/path
        resolved=path.resolve()
        if not any(base.resolve() in resolved.parents for base in [cfg.ROOT/'data/assets',cfg.STATE/'uploads']):raise ValueError('图片路径超出商品资料目录')
        notify(f'嵌入上传图片 {i}/{len(images)}')
        cli(['+cells-set-image','--spreadsheet-token',token,'--sheet-name','原始资料','--range','H'+str(task['row']),'--image',str(resolved.relative_to(cfg.ROOT))])
    notify('回读表头、样本ID和下拉选项')
    cells=cli(['+cells-get','--spreadsheet-token',token,'--sheet-id',batch['sheet_id'],'--range',f'A1:{col(len(columns(batch["rubric"]))-1)}{n}','--include','value,data_validation','--max-chars','20000000'])
    matrix=decode_cells(cells)
    expected=columns(batch['rubric'])
    if matrix[0]!=expected or {row[len(expected)-7] for row in matrix[1:]}!={s['sample_id'] for s in batch['samples']}:
        raise RuntimeError('回读样本或表头不一致，请检查导出表；不重复创建')
    # 服务端数据验证通过独立接口回读，不能只凭写操作成功。
    validation=cli(['+dropdown-get','--spreadsheet-token',token,'--sheet-id',batch['sheet_id'],'--range',f'D2:{col(3+2*(nd-1))}{n}'])
    verify_dropdowns(validation,n,nd)
    batch['export_status']='ready';batch['verified_at']=now();batch.pop('last_error',None);persist(batch);return summary(batch)

def verify_dropdowns(data,last_row,dimension_count):
    if data.get('has_more') or 'warning_message' not in data:
        raise RuntimeError('评分下拉回读不完整')
    found=set();expected={(r,col(3+2*i)) for r in range(2,last_row+1) for i in range(dimension_count)}
    for part in data.get('ranges',[]):
        if part.get('truncated') or not part.get('actual_range'):raise RuntimeError('评分下拉回读不完整')
        for r,cells in zip(part.get('row_indices',[]),part.get('cells',[])):
            for c,cell in zip(part.get('col_indices',[]),cells):
                if (r,c) not in expected:continue
                rule=cell.get('data_validation',{})
                if rule.get('type')=='list' and rule.get('items')==['0','1','2']:found.add((r,c))
    if found!=expected:raise RuntimeError('部分评分单元格缺少0/1/2下拉选项')

def decode_cells(data):
    if data.get('has_more'):raise ValueError('表格读取被截断，未导入评分；请缩小批次或提高读取预算')
    if 'warning_message' not in data:raise ValueError('表格缺少定位说明，未导入')
    rows=[]
    for part in data.get('ranges',[]):
        if not part.get('actual_range') or 'row_indices' not in part or 'col_indices' not in part:raise ValueError('表格缺少真实行列定位')
        for row,cs in zip(part['row_indices'],part['cells']):rows.append((row,[c.get('value','') for c in cs]))
    return [r for _,r in sorted(rows)]

def parse_ratings(batch,matrix):
    headers=columns(batch['rubric'])
    if not matrix or matrix[0]!=headers:raise ValueError('评审表头已改动，未导入；请保持原列名')
    indexes={h:i for i,h in enumerate(headers)};samples={s['sample_id']:s for s in batch['samples']};seen=set();out={};issues=[]
    for line,row in enumerate(matrix[1:],2):
        if not any(v not in ['',None] for v in row):continue
        if len(row)!=len(headers):raise ValueError('评审表列数不一致，未导入')
        sid=str(row[indexes['样本ID']]);s=samples.get(sid)
        if not s or sid in seen:raise ValueError('样本ID被修改、重复或新增了未知样本，未导入')
        seen.add(sid)
        expected=[sid,s['product_id'],s['session_id'],s['profile_revision'],s['result_revision'],batch['id'],batch['rubric']['version']]
        if [str(v) for v in row[-7:]]!=expected:raise ValueError('样本或版本标识被改动，未导入')
        # 防止人工改写待评文案后仍绑定到原快照打分；允许排序，不允许静默换产物。
        original=build_review_row(batch,s)
        if row[:3]!=original:raise ValueError('原始信息、档案或卖点列被改写，未导入；修改建议请写备注')
        scores={};gates={};notes={}
        for i,d in enumerate(batch['rubric']['gates']+batch['rubric']['dimensions']):
            value=row[3+2*i];notes[d['id']]=str(row[4+2*i] or '')
            if value in ['',None]:continue
            if type(value) is bool or str(value) not in ['0','1','2','0.0','1.0','2.0']:
                issues.append(f'第{line}行 {d["name"]}：只能填写0/1/2，当前值未接收');continue
            (gates if i<len(batch['rubric']['gates']) else scores)[d['id']]=int(float(value))
        complete=len(scores)==len(batch['rubric']['dimensions']) and len(gates)==len(batch['rubric']['gates'])
        out[sid]={'scores':scores,'gate_scores':gates,'dimension_notes':notes,
            'note':str(row[indexes['总体备注']] or ''),'reviewer':str(row[indexes['评审人']] or ''),
            'status':'complete' if complete else 'partial' if scores or gates else 'unrated'}
    if seen!=set(samples):raise ValueError('有样本行被删除，未导入；未评的行请保留空分数')
    return out,issues

def build_review_row(batch,s):
    mini={**batch,'samples':[s]};payload,_,_=build_workbook(mini)
    return payload['sheets'][0]['data'][0][:3]

def import_batch(batch,notify=lambda t:None):
    if batch.get('export_status')!='ready':raise ValueError('请先完成批量评审表导出')
    heads=columns(batch['rubric']);notify('读取整批人工评分')
    data=cli(['+cells-get','--spreadsheet-token',batch['sheet_token'],'--sheet-id',batch['sheet_id'],
        '--range',f'A1:{col(len(heads)-1)}{batch["sample_count"]+1}','--include','value','--max-chars','20000000'])
    matrix=decode_cells(data);ratings,issues=parse_ratings(batch,matrix)
    # 所有结构、身份和产物验证完成后再保存；缺分不当0，非法分数不当有效。
    previous=batch.get('ratings',{});batch['ratings']=ratings;batch['imported_at']=now()
    counts={k:sum(v['status']==k for v in ratings.values()) for k in ['complete','partial','unrated']}
    batch['progress']={**counts,'total':batch['sample_count'],'issues':issues}
    for s in batch['samples']:
        r=ratings[s['sample_id']]
        if r['status']!='complete' or (previous.get(s['sample_id'])==r):continue
        agent=SimpleNamespace(raw=s['raw'],knowledge=s['profile'],result=s['result'],config_revision=s['config_revision'],client=None,messages=[])
        session={'id':s['session_id'],'title':s['title'],'example':s['example'],'ui_messages':s['messages']}
        scored=score_report(r['scores'],r['gate_scores'],batch['rubric'])
        save_record(batch['owner'],session,{'rubric':batch['rubric'],**scored,'batch_id':batch['id'],'sample_id':s['sample_id'],
            'dimension_notes':r['dimension_notes'],'reviewer':r['reviewer'],'note':r['note']},'quality_score',agent=agent)
    batch['analysis']=analyze(batch);batch.pop('last_error',None);persist(batch);return summary(batch)

def analyze(batch):
    from .evaluation_store import ADVICE
    ratings=list(batch.get('ratings',{}).values());ds=[];advice=[]
    for d in batch['rubric']['dimensions']:
        values=[r['scores'][d['id']] for r in ratings if d['id'] in r['scores']]
        notes=[r['dimension_notes'].get(d['id'],'') for r in ratings if r['dimension_notes'].get(d['id'])]
        mean=round(sum(values)/len(values),2) if values else None
        ds.append({'id':d['id'],'name':d['name'],'n':len(values),'mean':mean,'distribution':{str(i):values.count(i) for i in [0,1,2]},'notes':notes})
        if mean is not None and mean<=1.2:
            target,text=ADVICE.get(d['id'],('生成策略','结合逐条备注定位问题'))
            advice.append({'dimension':d['name'],'n':len(values),'mean':mean,'target':target,'suggestion':text,'reviewer_notes':notes})
    gates={d['name']:{str(i):sum(r['gate_scores'].get(d['id'])==i for r in ratings) for i in [0,1,2]} for d in batch['rubric']['gates']}
    return {'dimensions':ds,'gates':gates,'suggestions':advice,
        'note':'按本批次已填单元格统计，n可能不同；空白不计0。仅完整评分写入全局记录；备注是改Prompt的线索，不自动修改。'}
