import argparse
import json
from pathlib import Path

from .contracts import ROOT, validate_shape
from .runtime import DeepSeekClient, SellingAgent


def main():
    parser = argparse.ArgumentParser(description='商品卖点Agent会话内核')
    parser.add_argument('--input', type=Path, help='原始商品输入JSON')
    parser.add_argument('--session', type=Path, help='会话JSON保存路径，存在时恢复')
    parser.add_argument('--validate-data', action='store_true', help='离线验证全部14条输入，无需密钥')
    args = parser.parse_args()
    if args.validate_data:
        manifest = json.loads((ROOT / 'data/manifest.json').read_text())
        for product in manifest['products']:
            validate_shape(json.loads((ROOT / product['input_file']).read_text()), 'input.schema.json')
        print('14条输入通过JSON Schema校验；这不表示内容真实性或卖点质量已评测。')
        return
    if not args.input and not (args.session and args.session.exists()):
        parser.error('请提供--input或已有--session')
    client = DeepSeekClient()
    if args.session and args.session.exists():
        agent = SellingAgent.restore(json.loads(args.session.read_text()), client)
    else:
        agent = SellingAgent(json.loads(args.input.read_text()), client)
    print('输入问题开始；/exit退出。资料检索、核对和卖点生成由Agent按意图选择。')
    while True:
        try:
            query = input('你> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if query == '/exit':
            break
        if not query:
            continue
        try:
            response = agent.turn(query)
            print('Agent>', response['message'])
            if response.get('stale'):
                print('提示：旧结果基于之前材料，需更新后使用。')
            if response.get('result') and not response.get('stale'):
                print(json.dumps(response['result'], ensure_ascii=False, indent=2))
        except Exception as exc:
            print('调用未完成：', str(exc))
        if args.session:
            args.session.parent.mkdir(parents=True, exist_ok=True)
            args.session.write_text(json.dumps(agent.snapshot(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
