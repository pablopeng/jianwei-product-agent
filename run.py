import argparse
import os
from agent.web import serve

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='启动商品卖点Agent本机Demo')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    args = parser.parse_args()
    serve(args.port, args.host)
