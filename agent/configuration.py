"""本机配置：密钥与可编辑业务规则分开保存，不向浏览器返回密钥。"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get('AGENT_STATE_DIR', str(ROOT / '.agent-state')))
LOCK = threading.RLock()
SCHEMAS = ['input.schema.json', 'product-knowledge.schema.json', 'selling-points.schema.json']
PROMPTS = ['agent', 'normalize', 'refine']


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.pending-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def overrides():
    path = STATE / 'config.json'
    return json.loads(path.read_text()) if path.exists() else {}


def documents():
    base = {
        'schemas': {n: json.loads((ROOT / 'schemas' / n).read_text()) for n in SCHEMAS},
        'prompts': {n: (ROOT / 'prompts' / (n + '.md')).read_text() for n in PROMPTS},
        'skill': (ROOT / 'skills/food-beverage/SKILL.md').read_text(),
        'subcategories': (ROOT / 'skills/food-beverage/references/subcategories.md').read_text(),
        'extra_skills': [],
        'evaluation': json.loads((ROOT / 'evaluation/rubric.json').read_text()),
    }
    custom = overrides()
    for key in ['schemas', 'prompts']:
        for name, value in custom.get(key, {}).items():
            if key == 'schemas' and name in ['product-knowledge.schema.json', 'selling-points.schema.json']:
                if value.get('$id') != base[key][name].get('$id'):
                    continue  # v1原配置保留在磁盘，不能覆盖新的v2运行合同。
            base[key][name] = value
    for key in ['skill', 'subcategories', 'extra_skills', 'evaluation']:
        if key in custom:
            base[key] = custom[key]
    generation_config = {k: v for k, v in base.items() if k != 'evaluation'}
    generation_config['quality_criteria'] = [{'name': d['name'], 'definition': d.get('definition', '')}
        for d in base['evaluation']['dimensions']]
    base['revision'] = hashlib.sha256(json.dumps(generation_config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return base


def protect_contract(candidate, baseline, path='$'):
    """可扩展字段/描述/约束，保留内核实际读取的核心字段及类型。"""
    if not isinstance(candidate, dict):
        raise ValueError(path + '必须是Schema对象')
    if 'type' in baseline and candidate.get('type') != baseline['type']:
        raise ValueError(path + '的核心字段类型不能更改')
    if 'const' in baseline and candidate.get('const') != baseline['const']:
        raise ValueError(path + '的核心常量不能更改')
    if not set(baseline.get('required', [])) <= set(candidate.get('required', [])):
        raise ValueError(path + '不能删除内核需要的必填字段')
    for section in ['properties', '$defs']:
        for key, value in baseline.get(section, {}).items():
            if key not in candidate.get(section, {}):
                raise ValueError(path + '.' + key + '是核心字段，不能删除')
            protect_contract(candidate[section][key], value, path + '.' + key)
    if isinstance(baseline.get('items'), dict):
        protect_contract(candidate.get('items'), baseline['items'], path + '[]')
    if '$ref' in baseline and candidate.get('$ref') != baseline['$ref']:
        raise ValueError(path + '的核心引用不能更改')
    if 'enum' in baseline and not set(baseline['enum']) <= set(candidate.get('enum', [])):
        raise ValueError(path + '不能删除内核使用的枚举值')


def save_document(kind, name, value):
    with LOCK:
        data = overrides()
        if kind == 'schema':
            if name not in SCHEMAS:
                raise ValueError('未知Schema')
            if isinstance(value, str):
                value = json.loads(value)
            Draft202012Validator.check_schema(value)
            protect_contract(value, json.loads((ROOT / 'schemas' / name).read_text()))
            data.setdefault('schemas', {})[name] = value
        elif kind == 'prompt':
            if name not in PROMPTS or not isinstance(value, str) or not value.strip():
                raise ValueError('Prompt名称或内容无效')
            data.setdefault('prompts', {})[name] = value
        elif kind in ['skill', 'subcategories']:
            if not isinstance(value, str) or len(value.strip()) < 20:
                raise ValueError('请填写完整规则')
            if kind == 'skill' and not value.startswith('---'):
                raise ValueError('Skill应保留YAML头信息')
            data[kind] = value
        elif kind == 'extra_skills':
            if not isinstance(value, list) or len(value) > 12:
                raise ValueError('额外Skill最多12个')
            seen = set()
            for item in value:
                if not isinstance(item, dict) or set(item) != {'id', 'name', 'content', 'enabled'}:
                    raise ValueError('Skill字段须包含id/name/content/enabled')
                if not re.fullmatch(r'[a-z0-9-]{1,60}', item['id']) or item['id'] in seen:
                    raise ValueError('Skill ID无效或重复')
                seen.add(item['id'])
                if not isinstance(item['enabled'], bool) or not isinstance(item['content'], str) or not item['content'].strip():
                    raise ValueError('Skill内容或启用状态无效')
            data[kind] = value
        elif kind == 'evaluation':
            if isinstance(value, str):
                value = json.loads(value)
            from .evaluation import validate_rubric
            validate_rubric(value)
            dimensions = value.get('dimensions', [])
            if not 1 <= len(dimensions) <= 10:
                raise ValueError('请配置1—10个维度')
            ids = set()
            for item in dimensions:
                if (not isinstance(item.get('weight'), (int, float)) or isinstance(item['weight'], bool)
                        or not 0 < item['weight'] <= 100 or not item.get('id') or item['id'] in ids
                        or not isinstance(item.get('name'), str) or not item['name'].strip()):
                    raise ValueError('维度ID需唯一，名称不能为空，权重须在0—100之间')
                ids.add(item['id'])
            data[kind] = value
        else:
            raise ValueError('未知配置类型')
        atomic_write(STATE / 'config.json', json.dumps(data, ensure_ascii=False, indent=2))
    return documents()


def model_config():
    values = {}
    path = ROOT / '.env.local'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
    return {name: os.environ.get(name, values.get(name, '')) for name in
            ['DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_VISION_MODEL']}


def save_model(key='', model='deepseek-flash', vision_model='deepseek-flash'):
    for value in [model, vision_model]:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
            raise ValueError('模型名称格式不正确')
    if key and (not isinstance(key, str) or len(key.strip()) < 12 or any(c.isspace() for c in key)):
        raise ValueError('密钥格式不正确，请粘贴完整密钥，不要包含空格')
    previous = model_config()
    actual = key.strip() if key else previous['DEEPSEEK_API_KEY']
    if not actual:
        raise ValueError('请先填写API Key')
    # 存储为用户本机文件，不进入可导出的业务配置。
    atomic_write(ROOT / '.env.local', 'DEEPSEEK_API_KEY=' + actual + '\nDEEPSEEK_MODEL=' + model +
                 '\nDEEPSEEK_VISION_MODEL=' + vision_model + '\n')


def public_model_config():
    values = model_config()
    return {'configured': bool(values['DEEPSEEK_API_KEY']),
            'model': values['DEEPSEEK_MODEL'] or 'deepseek-flash',
            'vision_model': values['DEEPSEEK_VISION_MODEL'] or values['DEEPSEEK_MODEL'] or 'deepseek-flash',
            'storage': '.env.local（本机服务端）',
            'environment_override': bool(os.environ.get('DEEPSEEK_API_KEY'))}
