#!/usr/bin/env bash
# 跑全部测试
set -e
cd "$(cd "$(dirname "$0")/.." && pwd)"
backend/.venv/bin/python -m pytest backend/tests "$@"
