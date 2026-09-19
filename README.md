# 快递管家 · 全流程 AI Agent

> 你说目标,它把事办完。

不是快递信息查询工具(那是菜鸟裹裹),也不是快递知识问答机器人(那是 ChatGPT 套壳)——
用户说一句「我快递坏了 / 我要退货 / 我要寄东西」,系统自己查轨迹、定责任、算赔偿、写话术、排跟进,
**到点还会主动来找你**。

技术栈:Vue 3 + FastAPI + MySQL(本地可用 SQLite)。

---

## 一分钟跑起来

```bash
cd ~/Desktop/express-agent && ./scripts/dev.sh
```

脚本会自动建虚拟环境、装依赖、生成配置文件,然后同时启动前后端。
打开 http://localhost:5173 即可。

> 没有配置大模型 API Key 也能跑:系统会自动降级到**演示模型**(规则引擎驱动),
> 三条流程、状态机、主动跟进、材料导出全部可演示,只是对话内容不会"思考"。

---

## 手动启动(两个终端)

**终端 1 · 后端**

```bash
cd ~/Desktop/express-agent && backend/.venv/bin/uvicorn main:app --reload --port 8000 --app-dir backend
```

**终端 2 · 前端**

```bash
cd ~/Desktop/express-agent/frontend && npm run dev
```

| 地址 | 说明 |
| --- | --- |
| http://localhost:5173 | 前端界面 |
| http://localhost:8000/docs | 后端 API 文档(Swagger) |

### 首次安装依赖

```bash
cd ~/Desktop/express-agent && python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt && (cd frontend && npm install)
```

> 前端需要 Node 18+。本机装的是 Node 20,若 `node -v` 显示 16,先执行:
> `export PATH="/opt/homebrew/opt/node@20/bin:$PATH"`

---

## 配置大模型

复制配置模板,填入任意一种 Key:

```bash
cp backend/.env.example backend/.env
```

### 用 LiteLLM 网关(推荐,一个 key 管所有模型)

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-你的LiteLLM虚拟key
OPENAI_BASE_URL=http://你的litellm地址:4000/v1
OPENAI_MODEL=你在 LiteLLM config 里配的 model_name
```

LiteLLM 暴露的就是标准 OpenAI 接口,所以走 `OPENAI_*` 三项即可,不用改代码。
注意 `OPENAI_BASE_URL` **结尾要带 `/v1`**。

### 其他方式

| 供应商 | 配置 |
| --- | --- |
| Claude 官方 | `ANTHROPIC_API_KEY=sk-ant-...`,`ANTHROPIC_MODEL=claude-opus-5` |
| DeepSeek | `OPENAI_BASE_URL=https://api.deepseek.com`,`OPENAI_MODEL=deepseek-chat` |
| 通义千问 | `OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`,`OPENAI_MODEL=qwen-plus` |
| Kimi | `OPENAI_BASE_URL=https://api.moonshot.cn/v1`,`OPENAI_MODEL=moonshot-v1-32k` |
| 智谱 | `OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4`,`OPENAI_MODEL=glm-4-plus` |

改完配置重启后端即可,界面右上角会显示当前使用的模型。

---

## 数据库

本地默认 SQLite,零配置:

```ini
DATABASE_URL=sqlite:///./data/app.db
```

推到服务器上换 MySQL(表结构启动时自动建):

```ini
DATABASE_URL=mysql+pymysql://用户名:密码@主机:3306/express_agent?charset=utf8mb4
```

建库语句:

```sql
CREATE DATABASE express_agent DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

---

## 三个 Agent 在做什么

### 索赔维权 Agent(核心)

用户输入一句「我快递坏了/丢了/延误了」,Agent 自主执行:

1. **查物流轨迹** —— 确认当前状态、异常节点、距上次更新多久
2. **定责任方** —— 网购件签收前风险在卖家(民法典 604 条),该找商家;自寄件才找快递公司。**很多人一上来就找错对象,白折腾一周**
3. **算赔偿区间** —— 保价按保价额,未保价用实际损失 + 格式条款规则(民法典 833/496/497 条)突破"只赔运费 3 倍"
4. **出材料** —— 交涉话术 + 证据清单,复制出去就能直接发
5. **排跟进** —— 报损后 7 天没答复自动提醒升级
6. **升级投诉** —— 客服 → 平台 → **12305 邮政业申诉(最管用)** → 12315 → 小额诉讼

### 退货管家 Agent

1. **判资格** —— 七天无理由 vs 质量问题(后者不受七天限制且运费归商家,这点最容易被商家糊弄)
2. **算成本** —— 运费谁出、有没有运费险、哪家寄回最便宜
3. **出话术** —— 直接复制发给商家
4. **主动跟进** —— 寄出第 3 天问签收,签收第 5 天问退款,没到账自动给催退款话术

### 寄件决策 Agent

1. **拆需求** —— 重量、体积、物品类型、时效
2. **比价** —— 7 个渠道对比,算**体积重**(长×宽×高÷6000 或 ÷8000,与实重取大者)
3. **给三个方案** —— 最便宜 / 最快 / 最稳,外加一个明确推荐
4. **避坑** —— 体积重、保价、禁寄限寄、打包规范

---

## 怎么演示"主动跟进"

主动提醒依赖真实时间(第 3 天、第 7 天),现场没法真等。顶栏有**演示时钟**:

1. 点一个场景跑完一轮,右侧面板会出现「待触发」的跟进
2. 点顶栏 **+3 天**
3. 后端立刻扫描到期跟进,Agent **自己在对话里发消息**,右侧跟进状态变成「已完成」

也可以在右侧面板对单条跟进点「立即触发」。

---

## 项目结构

```
express-agent/
├── backend/
│   ├── main.py                  FastAPI 入口
│   ├── app/
│   │   ├── config.py            配置(读 .env)
│   │   ├── clock.py             演示时钟(可快进)
│   │   ├── db.py / models.py    SQLAlchemy 模型
│   │   ├── events.py            进程内事件总线(推 SSE)
│   │   ├── export.py            材料 / 卷宗导出
│   │   ├── llm/                 模型适配层
│   │   │   ├── base.py          统一抽象(流式 + 工具调用)
│   │   │   ├── anthropic_client.py
│   │   │   ├── openai_client.py  ← LiteLLM / DeepSeek / 通义 等走这里
│   │   │   └── mock.py          没 Key 时的演示模型
│   │   ├── agent/
│   │   │   ├── tools.py         13 个工具的 schema + 实现
│   │   │   ├── prompts.py       三套系统提示词
│   │   │   ├── router.py        意图路由
│   │   │   └── runtime.py       工具循环(核心)
│   │   ├── domain/              业务引擎(不依赖大模型)
│   │   │   ├── claims.py        索赔评估
│   │   │   ├── returns.py       退货资格
│   │   │   ├── shipping.py      寄件比价
│   │   │   └── geo.py           省市解析
│   │   ├── rag/index.py         BM25(jieba)+ 可选向量,RRF 融合
│   │   ├── tasks/               任务状态机
│   │   ├── followup/scheduler.py 后台跟进调度
│   │   ├── tracking/provider.py  物流查询(快递100 / 演示数据)
│   │   └── routers/             API 路由
│   ├── knowledge/               10 篇知识库文档(约 12 万字)
│   └── tests/                   57 个测试
├── frontend/
│   └── src/
│       ├── App.vue              三栏主界面
│       ├── api.js               接口封装 + SSE 解析
│       └── components/          消息气泡 / 工具轨迹 / 任务面板 / 材料弹窗
└── scripts/dev.sh               一键启动
```

---

## 跑测试

```bash
cd ~/Desktop/express-agent && backend/.venv/bin/python -m pytest backend/tests -v
```

覆盖:体积重计费、偏远地区加价、七天无理由各类边界、质量问题免责、责任方判定、
彻底延误按丢失赔付、任务状态机跳步、跟进到期触发、卷宗导出、RAG 检索命中率、三条 Agent 端到端。

---

## 关于数据真实性

- **法律依据**:引自公开法规,知识库里写明了法律全称与条号,Agent 回答时必须先检索再引用。
- **价格与赔付倍数**:来自公开渠道的参考值,各网点、平台优惠差异大,系统在输出里都标注了"以官方公示为准"。
- **物流数据**:未配置快递100 密钥时使用**演示数据**,Agent 会在回答里说明这一点。
  配置 `KUAIDI100_KEY` / `KUAIDI100_CUSTOMER` 后自动切换为真实查询。

---

## 部署到服务器

```bash
# 后端
backend/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir backend

# 前端(构建静态文件,交给 nginx / caddy)
cd frontend && npm run build   # 产物在 frontend/dist
```

服务器上记得:改 `DATABASE_URL` 为 MySQL、填大模型 Key、在 `backend/main.py` 的
`CORSMiddleware` 里加上线上域名。
