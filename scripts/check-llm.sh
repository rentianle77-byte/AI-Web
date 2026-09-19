#!/usr/bin/env bash
# 检查 backend/.env 里的大模型配置是否可用。
# key 只在本机读取和使用,不会打印出来。
#
#   ./scripts/check-llm.sh          列出网关上可用的模型 + 试跑一次对话
#
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "✗ 找不到 $ENV_FILE,先执行:cp backend/.env.example backend/.env"
  exit 1
fi

# 只取需要的三项,不 source 整个文件
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*$//'; }
KEY="$(get OPENAI_API_KEY)"
BASE="$(get OPENAI_BASE_URL)"
MODEL="$(get OPENAI_MODEL)"

mask() { [ -z "$1" ] && echo "(空)" || echo "${1:0:6}…${1: -4}"; }

echo "读取 backend/.env:"
echo "  OPENAI_BASE_URL = ${BASE:-(空)}"
echo "  OPENAI_API_KEY  = $(mask "$KEY")"
echo "  OPENAI_MODEL    = ${MODEL:-(空)}"
echo

fail=0
if [ -z "$BASE" ] || [[ "$BASE" == *"某个地址"* ]] || [[ "$BASE" == *"你的litellm"* ]]; then
  echo "✗ OPENAI_BASE_URL 还没填。打开 LiteLLM 网页,把浏览器地址栏里 /ui 之前的部分抄下来,末尾加 /v1"
  fail=1
fi
if [ -z "$KEY" ] || [[ "$KEY" == *"粘贴"* ]]; then
  echo "✗ OPENAI_API_KEY 还没填(要 sk- 开头那串,不是页面上的 Key ID)"
  fail=1
fi
[ "$fail" = 1 ] && exit 1

case "$BASE" in
  */v1|*/v1/) ;;
  *) echo "⚠ OPENAI_BASE_URL 结尾没有 /v1,LiteLLM 通常需要。当前:$BASE"; echo ;;
esac

echo "==> 连接网关,列出可用模型…"
RESP="$(curl -s -m 15 -w $'\n__HTTP__%{http_code}' "${BASE%/}/models" -H "Authorization: Bearer $KEY" 2>&1)"
CODE="${RESP##*__HTTP__}"
BODY="${RESP%$'\n'__HTTP__*}"

if [ "$CODE" != "200" ]; then
  echo "✗ 请求失败(HTTP ${CODE:-无响应})"
  echo "$BODY" | head -5
  echo
  echo "常见原因:"
  echo "  401  → key 不对(别用页面上的 Key ID,要 sk- 开头那串)"
  echo "  404  → 地址少了 /v1,或者多了 /ui"
  echo "  超时 → 网关地址不对,或者需要连公司内网 / VPN"
  exit 1
fi

echo "✓ 网关连通。可以填进 OPENAI_MODEL 的名字:"
echo "$BODY" | python3 -c "
import json,sys
try:
    ids = sorted(m['id'] for m in json.load(sys.stdin).get('data', []))
except Exception:
    print('  (返回格式看不懂,原文如下)'); print(sys.stdin.read()[:500]); sys.exit()
if not ids:
    print('  (网关上没有配置任何模型,找管理这台 LiteLLM 的同事加)')
for i in ids:
    print('   -', i)
print()
print(f'共 {len(ids)} 个')
"

if [ -z "$MODEL" ] || [[ "$MODEL" == *"改成"* ]]; then
  echo
  echo "→ 从上面挑一个填进 backend/.env 的 OPENAI_MODEL,然后再跑一次本脚本做对话测试。"
  exit 0
fi

if ! echo "$BODY" | grep -q "\"$MODEL\""; then
  echo
  echo "⚠ OPENAI_MODEL=$MODEL 不在上面的列表里,调用会 404。请改成列表中的名字。"
  exit 1
fi

echo
echo "==> 用 $MODEL 试跑一次对话…"
cd "$ROOT"
backend/.venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, "backend")
from app.llm.factory import get_client

client = get_client()
print(f"   provider={client.provider}  model={client.model}")
if client.provider == "mock":
    print("✗ 仍然是演示模型 —— 说明 LLM_PROVIDER 不是 openai,或者 OPENAI_API_KEY 为空")
    sys.exit(1)

async def main():
    got, err = [], None
    async for ev in client.astream(
        "你是一个测试助手,用不超过 15 个字回答。",
        [{"role": "user", "content": "用一句话说明你是谁"}],
        None,
    ):
        if ev.type == "text_delta":
            got.append(ev.text)
        elif ev.type == "error":
            err = ev.text
    if err:
        print(f"✗ 调用失败:{err}")
        return 1
    if not "".join(got).strip():
        print("✗ 模型没有返回内容")
        return 1
    print(f"   模型回复:{''.join(got).strip()}")
    print("\n✓ 配置可用,直接 ./scripts/dev.sh 启动即可。")
    return 0

sys.exit(asyncio.run(main()))
PY
