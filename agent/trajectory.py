"""只评公开工具轨迹与交互，不读取或输出模型内部思考。"""
import json
from collections import Counter

TRAJECTORY_RUBRIC = {'version': 'trajectory-1.0', 'score_range': [0, 2], 'gates': [], 'dimensions': [
    {'id': 'intent', 'name': '意图与任务完成', 'weight': 25, 'definition': '响应当前用户意图，完成请求或明确暂停原因，不强制执行无关阶段。', 'anchors': {'0': '答非所问、漏做核心任务或宣称完成未完成工作。', '1': '方向正确，但有漏项或完成状态不清。', '2': '处理范围和结果符合请求，未完成部分说明明确。'}},
    {'id': 'tools', 'name': '工具选择与证据使用', 'weight': 25, 'definition': '按问题选择检索、核查、建档、修改或生成，并使用正确来源。', 'anchors': {'0': '用错工具、无依据生成或把资料指令当命令。', '1': '工具基本合适，但检索不足或有无效步骤。', '2': '工具与问题匹配，证据足够且范围正确。'}},
    {'id': 'interaction', 'name': '确认与多轮协作', 'weight': 20, 'definition': '建档、修改后等待用户确认；追问有必要，能够继承用户上下文。', 'anchors': {'0': '越过确认、擅改事实或丢失明确约束。', '1': '未越界，但有重复追问或上下文衔接不佳。', '2': '该确认时确认，该继续时继续，修改准确承接。'}},
    {'id': 'efficiency', 'name': '执行效率', 'weight': 15, 'definition': '在必要证据充分的前提下避免重复调用和无效循环；调用少不一定更好。', 'anchors': {'0': '循环或重复耗尽预算，影响任务完成。', '1': '任务完成，但存在明显可省的重复。', '2': '步骤必要且紧凑，批量与复用合理。'}},
    {'id': 'recovery', 'name': '错误处理与结果诚实', 'weight': 15, 'definition': '识别失败、修复或说明限制，不隐瞒错误或编造成功。', 'anchors': {'0': '失败后仍宣称成功或制造结果。', '1': '说明错误但恢复不充分，或状态表达含糊。', '2': '错误被正确处理或诚实停止；无错误时也准确报告结果。'}}]}


def review_trajectory(agent):
    trace = agent.trace
    generated = [t for t in trace if t['tool'] == 'generate_selling_points' and t.get('ok')]
    unknown = [t for t in generated if 'confirmed_before' not in t]
    violations = [t for t in generated if t.get('confirmed_before') is False and t.get('profile_contract') == 2]
    counts = Counter((t.get('turn_id', 'legacy'), t['tool'], json.dumps(t.get('arguments', {}), sort_keys=True, ensure_ascii=False))
                     for t in trace if 'turn_id' in t)
    repeats = sum(max(0, n - 2) for n in counts.values())
    failures = sum(not t.get('ok', False) for t in trace)
    checks = [
        {'name': '生成前确认', 'status': 'failed' if violations else 'review' if unknown else 'passed' if generated else 'not_applicable',
         'detail': f'成功生成{len(generated)}次；发现越过确认{len(violations)}次；缺少历史确认状态{len(unknown)}次。'},
        {'name': '同轮完全相同请求重复', 'status': 'review' if repeats else 'passed' if any('turn_id' in t for t in trace) else 'not_applicable',
         'detail': f'同轮相同工具和参数超过两次的调用有{repeats}次；是否必要需人工结合任务判断。'},
        {'name': '错误处理复核', 'status': 'review' if failures else 'passed' if trace else 'not_applicable',
         'detail': f'工具失败{failures}次。发生失败不等于整体失败，需要核对后续恢复与公开回复。'},
        {'name': '意图、证据与完成质量', 'status': 'review', 'detail': '需要结合用户问题、工具输入输出与最终回复人工评分；规则不能证明答对或证据充分。'}]
    return {'type': 'trajectory', 'checks': checks, 'trace': trace,
            'metrics': {'tool_calls': len(trace), 'failures': failures, 'duplicate_excess': repeats,
                        'success_rate': round((len(trace)-failures)/len(trace), 4) if trace else None,
                        'measured_tool_ms': round(sum(t.get('duration_ms', 0) for t in trace), 1),
                        'timed_calls': sum('duration_ms' in t for t in trace)},
            'rubric': TRAJECTORY_RUBRIC,
            'note': '规则检查覆盖已记录的公开工具轨迹；旧轨迹缺字段不判通过。工具成功率与工具耗时不代表任务质量或端到端响应时间。'}
