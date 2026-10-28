#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
printf '打开浏览器访问 http://127.0.0.1:8765\n'
exec .venv/bin/python run.py
