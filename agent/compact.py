"""精简的模型数据合同；来源及版本元数据由运行时单独管理。"""
import copy
import json
import re
import unicodedata

from .contracts import revision, schema, validate_shape
from .configuration import documents


def public(value):
    return {k: v for k, v in value.items() if k != '_meta'}


def leaves(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [s for v in value for s in leaves(v)]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k != '_meta' for s in leaves(v)]
    return []


def normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).lower()


def extract_json(agent, prompt, payload, schema_name, check, images=None):
    message = json.dumps(payload, ensure_ascii=False)
    content = [{'type': 'text', 'text': message}] + images if images else message
    system = prompt + '\n业务输出Schema：\n' + json.dumps(schema(schema_name), ensure_ascii=False)
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]
    for attempt in range(2):
        response = agent.client.complete(messages, structured=True, vision=bool(images))
        try:
            candidate = json.loads(response.get('content') or '')
            return check(candidate)
        except Exception as exc:
            agent.last_validation_error = str(exc)
            agent.last_validation_candidate = response.get('content') or ''
            if attempt:
                raise ValueError('本次整理未完成，已保留原始资料，可重试。') from None
            messages.extend([{'role': 'assistant', 'content': response.get('content') or ''},
                {'role': 'user', 'content': '修复JSON结构后重试。不要要求用户补充信息来解决格式问题：' + str(exc)[:1000]}])


def material_batches(raw):
    """覆盖所有内容，不以截断方式压入单次上下文。"""
    batches, batch, chars, images = [], [], 0, 0
    for material in raw['materials']:
        text = material['text']
        chunks = [text[i:i + 24000] for i in range(0, len(text), 24000)] or ['']
        for index, chunk in enumerate(chunks):
            size, is_image = len(chunk), material['kind'] == 'image'
            if batch and (chars + size > 42000 or images + is_image > 10):
                batches.append(batch)
                batch, chars, images = [], 0, 0
            entry = {**material, 'text': chunk}
            if len(chunks) > 1:
                entry['locator'] += ':text-part=' + str(index + 1)
            batch.append(entry)
            chars += size
            images += is_image
    if batch:
        batches.append(batch)
    return batches


def classify_sources(ids, raw, viewed):
    found = {m['id']: m for m in raw['materials']}
    valid = [sid for sid in ids if isinstance(sid, str) and sid in found
             and found[sid]['origin'] != 'ai' and found[sid]['kind'] != 'ai_derived'
             and (found[sid]['kind'] != 'image' or sid in viewed)]
    if not valid:
        return [], 'unsupported'
    if all(found[s]['origin'] == 'buyer' or found[s]['kind'] == 'buyer_review' for s in valid):
        return valid, 'feedback'
    if all(found[s]['kind'] in ['marketing_copy', 'buyer_review'] or found[s]['origin'] == 'buyer' for s in valid):
        return valid, 'description'
    return valid, 'fact'


def validate_profile(profile, raw):
    validate_shape(public(profile), 'product-knowledge.schema.json')
    meta = profile.get('_meta', {})
    if meta.get('product_id') != raw['product_id'] or meta.get('material_revision') != revision(raw):
        raise ValueError('商品档案已过期，请重新整理')
    source_ids = {m['id'] for m in raw['materials']}
    for item in meta.get('sources', {}).values():
        if not set(item['source_ids']) <= source_ids:
            raise ValueError('来源引用不存在')
    return profile


def normalize(agent):
    batches = material_batches(agent.raw)
    all_images, viewed = agent.image_blocks()
    # 图片块按source_id索引，批次只携带本次真实材料对应的图片。
    image_map = {all_images[i]['text'].split('=', 1)[1]: all_images[i:i + 2] for i in range(0, len(all_images), 2)}
    prompt = documents()['prompts']['normalize']
    prompt += ('\n只输出业务Schema定义的JSON。每条有来源的信息，在字符串末尾附上〔source_id〕，'
               '多来源可连续附多个标记，例如"-18℃冷冻保存〔attr-2〕"。后台会剥离标记并保存来源。'
               '规格名称、配料、属性、原有卖点、买家反馈也这样标注。不用输出额外的sources对象、逐字证据或元数据。'
               '摘要可以归纳，不要求与原文逐字相同。不能只用旧AI内容作为依据。')
    partials = []
    def check(value):
        if 'profile' not in value:
            value = {'profile': value}
        validate_shape(value['profile'], 'product-knowledge.schema.json')
        if not isinstance(value.get('sources'), list):
            value['sources'] = []
        return value
    for index, materials in enumerate(batches):
        if agent.event_callback and len(batches) > 1:
            agent.event_callback('分批整理资料 ' + str(index + 1) + '/' + str(len(batches)))
        images = [block for m in materials for block in image_map.get(m['id'], [])]
        partials.append(extract_json(agent, prompt, {'materials': materials,
            'product_name_hint': agent.raw['display_name'], 'batch': index + 1, 'batch_total': len(batches)},
            'product-knowledge.schema.json', check, images))
    merged = partials[0]
    if len(partials) > 1:
        if agent.event_callback:
            agent.event_callback('合并档案并隔离冲突')
        merged = extract_json(agent, prompt + '\n合并各批档案，去重但保留不同规格和不同维度。'
            '不同值先检查是否同一SKU同一口径；真正矛盾的内容移出明确属性，单列待确认，不任选一个。'
            '不要把本批未提供的信息说成整个商品缺失；sources引用仍用原始source_id。',
            {'partial_profiles': partials}, 'product-knowledge.schema.json', check)
        if agent.event_callback:
            agent.event_callback('复核规格范围与档案一致性')
        merged = extract_json(agent, prompt + '\n对已合并档案做最后一致性复核。'
            '同一口味/套装在不同批次出现的别名应合并，不把参数表中的同口味另建一条SKU。'
            '某一口味独有的配料只放在该规格details，不能放进全商品通用配料；全部配料不确定时通用配料留空。'
            '图文中的主观口感、工艺宣传、清真等标识如无独立证明，归入原有卖点，不升级为认证承诺。'
            '待确认项必须与完整档案一致：已被其他明确字段补足的缺口不再列为缺失。'
            '所有有价值的确定信息及原有宣传、反馈仍要保留，不为缩短删掉已知内容。保留来源标记。',
            {'merged_profile': merged}, 'product-knowledge.schema.json', check)
    profile = merged['profile']
    sources = {}
    source_ids = {m['id'] for m in agent.raw['materials']}
    def clean_annotations(value):
        if isinstance(value, dict):
            return {k: clean_annotations(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean_annotations(v) for v in value]
        if not isinstance(value, str):
            return value
        ids = []
        def remove(match):
            candidates = re.split(r'[,，、\s]+', match.group(1))
            valid = [sid for sid in candidates if sid in source_ids]
            if not valid:
                return match.group(0)
            ids.extend(valid)
            return ''
        cleaned = re.sub(r'[〔\[]([^〕\]]+)[〕\]]', remove, value).strip()
        valid, kind = classify_sources(ids, agent.raw, viewed)
        if valid:
            sources[cleaned] = {'source_ids': list(dict.fromkeys(valid)), 'kind': kind}
        return cleaned
    profile = clean_annotations(profile)
    for part in partials + [merged]:
        for item in part.get('sources', []):
            if not isinstance(item, dict) or not isinstance(item.get('item'), str) or not isinstance(item.get('source_ids'), list):
                continue
            ids, kind = classify_sources(item['source_ids'], agent.raw, viewed)
            if ids:
                sources[item['item']] = {'source_ids': list(dict.fromkeys(ids)), 'kind': kind}
    # 摘要引用的检查与归类不再要求逐字原文；缺引用时只做可证明的文本匹配。
    for text in leaves(profile):
        if text in sources or text in profile['cautions']:
            continue
        matches = [m['id'] for m in agent.raw['materials'] if text and normalized(text) in normalized(m['text'])]
        ids, kind = classify_sources(matches, agent.raw, viewed)
        if ids:
            sources[text] = {'source_ids': ids, 'kind': kind}
    excluded = []
    def known(items, expected):
        accepted = []
        for item in items:
            support = sources.get(item)
            if not support:
                excluded.append(item)
            elif expected == 'fact' and support['kind'] in ['description', 'feedback']:
                target = 'buyer_feedback' if support['kind'] == 'feedback' else 'original_selling_points'
                if item not in profile[target]:
                    profile[target].append(item)
            else:
                accepted.append(item)
        return list(dict.fromkeys(accepted))
    info = profile['product_info']
    info['ingredients'] = known(info['ingredients'], 'fact')
    info['attributes'] = known(info['attributes'], 'fact')
    profile['specifications'] = [{'name': spec['name'], 'details': known(spec['details'], 'fact')}
        for spec in profile['specifications'] if spec['name'] in sources]
    profile['original_selling_points'] = known(profile['original_selling_points'], 'description')
    profile['buyer_feedback'] = known(profile['buyer_feedback'], 'feedback')
    flavor_variants = False
    for material in agent.raw['materials']:
        if material['kind'] != 'specification':
            continue
        try:
            spec_data = json.loads(material['text'])
            flavor_variants = flavor_variants or any(
                any(word in name for word in ['口味', '品种']) and len(values) > 1
                for name, values in zip(spec_data.get('child_spec_seq', []), spec_data.get('spec_values_seq', [])))
        except (ValueError, TypeError):
            pass
    if flavor_variants and info['ingredients']:
        # 多口味且并非每款都提供配方时，不能把其中一款的配料变成全商品通用配料。
        recipes = ['；'.join(detail for detail in spec['details'] if '配料' in detail) for spec in profile['specifications']]
        common = [item for item in info['ingredients'] if recipes and all(normalized(item) in normalized(recipe) for recipe in recipes)]
        scoped = [item for item in info['ingredients'] if item not in common]
        if scoped:
            profile['cautions'].append('资料中出现的配料为：' + '、'.join(scoped) +
                '。部分口味没有独立配料信息，未将这组配料作为全商品通用配料；已明确对应的配方保留在规格信息中。')
            info['ingredients'] = common
    for item in profile['original_selling_points']:
        if item in sources:
            sources[item]['kind'] = 'description'
    for item in profile['buyer_feedback']:
        if item in sources:
            sources[item]['kind'] = 'feedback'
    if excluded:
        profile['cautions'].append('以下摘要尚未找到可靠来源，未纳入明确属性：' + '；'.join(dict.fromkeys(excluded)))
    profile['cautions'] = list(dict.fromkeys(profile['cautions']))
    profile['_meta'] = {'contract_version': 2, 'product_id': agent.raw['product_id'],
        'material_revision': revision(agent.raw), 'sources': sources, 'batches': len(batches)}
    validate_profile(profile, agent.raw)
    agent.knowledge, agent.result, agent.viewed_images = profile, None, viewed
    return profile


def validate_points(result, profile):
    validate_shape(public(result), 'selling-points.schema.json')
    meta = result.get('_meta', {})
    if meta.get('knowledge_revision') != revision(profile) or meta.get('material_revision') != profile['_meta']['material_revision']:
        raise ValueError('卖点基于过期档案')
    allowed = set(leaves({k: v for k, v in public(profile).items() if k != 'cautions'}))
    for record in meta.get('support', []):
        if not record['basis'] or not set(record['basis']) <= allowed:
            raise ValueError('卖点引用了未确认的信息')
    if len(meta.get('support', [])) != len(result['selling_points']):
        raise ValueError('部分卖点缺少依据')
    return result


def generate(agent, instruction, target_sku_ids, focus_point_id):
    profile = agent.knowledge
    previous = agent.result if agent.result and 'selling_points' in agent.result else None
    focus_index = None
    if focus_point_id:
        try:
            focus_index = int(focus_point_id.removeprefix('point-')) - 1
        except ValueError:
            raise ValueError('要修改的卖点编号不正确')
        if not previous or not 0 <= focus_index < len(previous['selling_points']):
            raise ValueError('该条卖点不存在')
    prompt = agent.load_skill() + ('\n只输出业务Schema的JSON。在每条卖点text末尾附上使用的档案信息编号，例如〔F3〕或〔F3,F4〕。'
        '这些是后台依据标记，程序会自动去掉。不要另写support、来源原文或元数据。'
        '不预设卖点条数，信息充分时多写，去重后按购买决策价值排序；priority为正整数，1最高。'
        'cautions里指出的冲突数值或声称即使也出现在属性中，仍须避开，不能任选一个版本；其他有据内容继续生成。'
        '规格不同则明确写适用规格，不将某口味的配料用于其他口味。'
        '主观口感可用商品页原有描述，但不能升级为健康、功效、认证保证。'
        '使用buyer_feedback时需归因为买家反馈。只有含认证文件证据的档案才能承诺认证。')
    prompt += ('默认卖点不写具体烹饪分钟数，重点说明已明确的做法与免解冻等便利。'
               '用户明确索要烹饪指导时应回查原始说明，不能把炸制时间移用于煎制。'
               '不要把配料未知的款式写成拥有另一款的配方。'
               '生动表达不能增加档案没有的商品性状、检测结论或到货保证。'
               '输出前逐句删除无依据的新增细节；使用短标题与展开正文时，两部分都必须有依据。')
    rubric = documents()['evaluation']
    prompt += '\n写完后按这些质量标准检查整组，修复后只返回文案，不返回自评分：' + json.dumps([{'name': d['name'], 'definition': d.get('definition', '')} for d in rubric['dimensions']], ensure_ascii=False)
    allowed = set(leaves({k: v for k, v in public(profile).items() if k != 'cautions'}))
    fact_codes = {f'F{i+1}': value for i, value in enumerate(dict.fromkeys(leaves(public(profile))))
                  if value in allowed and value in profile['_meta']['sources']}
    by_value = {value: code for code, value in fact_codes.items()}
    def annotate(value):
        if isinstance(value, str):
            return value + ('〔' + by_value[value] + '〕' if value in by_value else '')
        if isinstance(value, list):
            return [annotate(v) for v in value]
        if isinstance(value, dict):
            return {k: annotate(v) if k != 'cautions' else v for k, v in value.items()}
        return value
    def check(value):
        if 'output' not in value:
            value = {'output': value}
        if 'support' not in value:
            supports = []
            for point in value['output'].get('selling_points', []):
                codes = []
                def strip_code(match):
                    codes.extend(code for code in re.split(r'[,，、\s]+', match.group(1)) if code in fact_codes)
                    return ''
                point['text'] = re.sub(r'[〔\[]([^〕\]]+)[〕\]]', strip_code, point['text']).strip()
                basis = list(dict.fromkeys(fact_codes[code] for code in codes))
                kinds = {profile['_meta']['sources'][b]['kind'] for b in basis}
                kind = 'fact' if kinds <= {'fact'} else 'feedback' if kinds <= {'feedback'} else 'description'
                if kind == 'feedback' and not any(word in point['text'] for word in ['买家', '评价', '反馈']):
                    point['text'] = '部分买家反馈：' + point['text']
                supports.append({'basis': basis, 'kind': kind})
            value['support'] = supports
        output = value['output']
        validate_shape(output, 'selling-points.schema.json')
        if not isinstance(value.get('support'), list) or len(value['support']) != len(output['selling_points']):
            raise ValueError('每条卖点都要配一条后台依据')
        kept, supports = [], []
        for point, support in zip(output['selling_points'], value['support']):
            basis = support.get('basis', [])
            if not basis or not set(basis) <= allowed:
                raise ValueError('依据请引用档案中的完整信息，不引用待确认项')
            kinds = [profile['_meta']['sources'].get(b, {}).get('kind') for b in basis]
            if support.get('kind') == 'fact' and any(k in ['description', 'feedback'] for k in kinds):
                raise ValueError('宣传与评价不能作为客观事实；可改用适当归因的表达')
            ids = list(dict.fromkeys(s for b in basis for s in profile['_meta']['sources'].get(b, {}).get('source_ids', [])))
            if not ids:
                raise ValueError('此条卖点没有可靠来源，请换用其他已确认信息')
            kept.append(point)
            supports.append({'basis': basis, 'source_ids': ids, 'kind': support.get('kind', 'description')})
        order = sorted(range(len(kept)), key=lambda i: kept[i]['priority'])
        output['selling_points'] = [kept[i] for i in order]
        supports = [supports[i] for i in order]
        if focus_index is not None:
            if len(kept) != len(previous['selling_points']):
                raise ValueError('局部改写须保留原有卖点数量')
            if any(point != previous['selling_points'][i] for i, point in enumerate(output['selling_points']) if i != focus_index):
                raise ValueError('只能修改指定条目，其他条目保持原样')
        output['_meta'] = {'contract_version': 2, 'product_id': agent.raw['product_id'],
            'material_revision': revision(agent.raw), 'knowledge_revision': revision(profile),
            'support': supports, 'target_specifications': target_sku_ids}
        return validate_points(output, profile)
    result = extract_json(agent, prompt, {'product_profile': annotate(public(profile)), 'instruction': instruction,
        'target_specifications': target_sku_ids, 'focus_index': focus_index,
        'previous_output': public(previous) if previous else None}, 'selling-points.schema.json', check)
    draft = copy.deepcopy(public(result))
    for point, support in zip(draft['selling_points'], result['_meta']['support']):
        point['text'] += '〔' + ','.join(by_value[b] for b in support['basis'] if b in by_value) + '〕'
    if agent.event_callback:
        agent.event_callback('复核表达中的新增承诺与重复卖点')
    result = extract_json(agent, prompt + '\n' + documents()['prompts']['refine'],
        {'product_profile': annotate(public(profile)), 'draft': draft, 'instruction': instruction,
         'focus_index': focus_index, 'previous_output': public(previous) if previous else None},
        'selling-points.schema.json', check)
    agent.result = result
    return result
