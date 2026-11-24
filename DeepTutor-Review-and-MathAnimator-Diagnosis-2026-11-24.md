# DeepTutor 项目分析 · 产品设计 · math_animator 故障诊断

> 会话记录整理 · 2026-11-24
> 仓库：`~/DeepTutor`（HKUDS/DeepTutor v1.7.4，main 分支，commit `1284116f9`）
> 规模：33.7 万行 Python / 1612 个 TS/TSX 文件 / 全库 335MB

---

## 目录

1. [项目架构分析](#一项目架构分析)
2. [产品设计分析](#二产品设计分析)
3. [math_animator 故障诊断](#三math_animator-故障诊断)
4. [修复方案与优先级](#四修复方案与优先级)

---

## 一、项目架构分析

### 1.1 定位

HKUDS 出品的「agent-native 智能学习伴侣」——AI 辅导/教学平台。Apache-2.0 许可。
版本单一事实源在 `deeptutor/__version__.py`（`__version__ = "1.7.4"`）。

### 1.2 架构主线：三层收敛

所有能力收敛到一条主干：

```
CLI (Typer) ┐
WebSocket   ├→ TurnApplicationService → TurnEngine → ChatOrchestrator → Capability
Python SDK  ┘        (持久化/重放/取消)      (路由 UnifiedContext)          ↓
                                                              ToolRegistry (L1)
                                                              CapabilityRegistry (L2)
```

关键设计取舍：

- **入口必须走应用服务**。`docs-for-dev/ARCHITECTURE.md` 明确禁止绕过 `deeptutor/app/service.py` 直接实例化 orchestrator。持久化、提交身份、重放、取消、恢复全部归应用/运行时层所有，避免了「CLI 能跑但 Web 恢复不了」这类分裂。
- **两级插件模型**：
  - L1 = LLM 按需选择的单函数工具，8 个用户可开关：`brainstorm` / `web_search` / `paper_search` / `zotero_search` / `reason` / `geogebra_analysis` / `imagegen` / `videogen`。权威列表在 `deeptutor/tools/builtin/__init__.py` 的 `USER_TOGGLEABLE_TOOL_NAMES`。
  - L2 = 接管整轮的多阶段 Capability：`chat`（默认）/ `ask_questions` / `deep_solve` / `deep_question` / `deep_research` / `visualize` / `math_animator` / `mastery_path` / `immersive_reading` / `video_lesson`。
  - 边界由 `deeptutor/agents/_shared/tool_composition.py` 的 `ToolMountFlags` 统一裁决，不散落在各 capability 里。
- **统一结果信封**：所有 capability 经 `deeptutor/agents/_shared/capability_result.py` 的 `emit_capability_result()` 出口，带 `UsageTracker` 的成本汇总。成本可观测性是内建的。

### 1.3 关键文件

| 路径 | 职责 |
|---|---|
| `deeptutor/runtime/orchestrator.py` | `ChatOrchestrator` 统一入口（277 行） |
| `deeptutor/runtime/turn_engine.py` | 下层执行（43 行） |
| `deeptutor/app/service.py` | 持久化准入、执行、重放（683 行） |
| `deeptutor/app/facade.py` | SDK 门面（279 行） |
| `deeptutor/core/capability_protocol.py` | `TurnCapability` + `CapabilityManifest`（102 行） |
| `deeptutor/core/tool_protocol.py` | `BaseTool` + `ToolDefinition`（329 行） |
| `deeptutor/core/context.py` | `UnifiedContext` |
| `deeptutor/api/routers/unified_ws.py` | 统一 WebSocket 端点 |
| `deeptutor/services/session/turns/executor.py` | 轮次执行器 |
| `deeptutor/services/session/context_builder.py` | 历史预算构建 |

### 1.4 工程化水平

这是项目最扎实的部分：

| 手段 | 具体做法 |
|---|---|
| 分层约束 | `.importlinter` 三条硬契约：① core 只允许「值 + 协议」；② runtime/services/knowledge/learning/multi_user/capabilities/agents 不得反向 import api；③ app 不得 import api |
| 前端约束 | dependency-cruiser 做 `architecture:check`；route budget 性能预算 `perf:check` |
| 质量门禁 | `check:fast` = 契约生成校验 → 架构 → 类型 → node 测试 → 单测 → lint → i18n 一致性 |
| 安全 | detect-secrets 基线、bandit、`scripts/check_repo_hygiene.py`、模块插件双重审查门 |
| 测试量 | 1017 个 Python 测试文件 + 221 个前端测试文件，含 Playwright 的 critical-turns / multi-worker 项目 |

CI 有 8 条 workflow：`tests` / `pypi-release` / `docker-release` / `desktop-release` / `modules` / `memory-evaluation` / `repository-hygiene` / `desktop-notarization-diagnostics`。
桌面端覆盖 macOS arm64 + x64 + Windows 三目标（`apps/desktop/platforms/`）。

### 1.5 模块化打包（1.7 核心重构）

`deeptutor/modules/manifests/` 有 25 个模块 manifest，`installed / enabled / ready` 三态分离：

```
audio-overview, books, calendar, core, co-writer, knowledge, mastery, memory,
notebook, parser-docling, parser-liteparse, parser-markitdown, parser-pymupdf4llm,
parsing, partners, providers, questions, reading, renderer-manim, research,
server, solve, video-lesson, visualize, web
```

核心 wheel 只含 CLI + SDK，其余按需安装（`deeptutor[full]` 是安装 profile 而非耦合）。
**代价**：版本必须全家桶对齐，`deeptutor-*=={version}` 全部锁死同版本。

### 1.6 技术栈

- **后端**：Python ≥3.11,<3.15，FastAPI + aiosqlite + LlamaIndex + Typer + Pydantic v2 + MCP
- **前端**：Next.js 16 + React 19 + Tailwind + i18next（**13 种语言**：de/en/fr/hu/it/ja/ko/pl/pt/tr/uk/vi/zh）
- **桌面**：Electron 壳 + 内嵌 core
- **渲染**：Manim（数学动画）、Remotion（视频课）、KaTeX、Mermaid、Chart.js、PDF.js、epubjs、BlockNote

### 1.7 风险点

1. **巨型文件**（分层好但单文件粒度失控）：
   - `api/routers/knowledge.py` 5294 行
   - `services/session/sqlite_store.py` 4921 行
   - `capabilities/mastery/tools.py` 4012 行
   - `learning/service.py` 3426 行
   - `agents/research/pipeline.py` 3281 行
2. **mypy 基本关着**：`--ignore-missing-imports --no-strict-optional`，且排除了 `agents/`、`services/rag/`、`api/routers/` 三个最易错目录。类型安全实际靠 Pydantic 运行时兜底。
3. **i18n 是 13 语言 × 全站文案**：parity 脚本只能查键缺失，查不了语义漂移，长期隐性负债。
4. **发布节奏极快**：1.4.0 → 1.7.4 几乎每周多版，代码腐化压力大。
5. **仓库体积偏大**：`web/` 28MB、`assets/` 25MB，对 clone 和 CI 不友好。

### 1.8 总评

架构纪律罕见的成熟开源项目。最值得学习的是「用 importlinter + dependency-cruiser + 契约生成 + route budget 把架构意图变成 CI 可执行约束」——大部分项目只写在文档里。

最该改进：**文件拆分** + **类型检查**（在 `services/` 和 `api/routers/` 上真正打开 mypy，哪怕先只查新增代码）。

---

## 二、产品设计分析

### 2.1 核心定位：不做「AI 问答」，做「有教学法的学习系统」

与 ChatGPT 套壳产品的根本分野。证据在 `deeptutor/learning/policy.py` 的文档字符串：

> The gate is the heart of mastery-based learning. An objective only counts as mastered when the evidence clears its threshold, and `next_objective` **keeps returning the same objective until it does** — advancement is *computed from what is mastered*, never tracked by a stage counter.

翻译成产品语言：**它拒绝让你「点下一步」**。进度不是计数器，是从证据算出来的。
源码里还直接写了参照对象——`~0.9 mirrors Alpha School's "90% before you advance"`。

### 2.2 四条学习主线

定义在 `web/components/learning/surfaces.ts`：

| 主线 | 用户心智 | 差异化点 |
|---|---|---|
| **Books** | 「让 AI 给我写本书」 | **先定章节目录，再写正文**——可在写一个字之前改章节骨架 |
| **Mastery Path** | 「我要掌握某个领域」 | 大纲**在对话中协商确定**，然后变成知识点地图逐点攻克 |
| **Practice** | 「我的题库 + 错题本 + 每日复习」 | 聊天/书/掌握路径产生的题**自动汇入**，还能从表格导入 |
| **Immersive Reading** | 「带着家教读书」 | 选中文段直接提问，支持 PDF/EPUB/网页/视频/音频 |

**关键产品决策：Practice 是收口。** 三条学习路径产出的题目全部沉淀到同一个题库，用统一的间隔重复调度（`deeptutor/services/practice/scheduler.py`）。这解决了 AI 教育产品最大的留存问题——学完就散，没有资产沉淀。

### 2.3 教学设计的三处硬核细节

**① 按知识类型分流评测方式**（`learning/policy.py`）

```python
QUANTITATIVE_GATE = {MEMORY: 0.9, PROCEDURE: 0.9}   # 量化：近期加权准确率
QUALITATIVE_TYPES = {CONCEPT, DESIGN}                # 质化：Feynman 式讲解，导师判定
```

记忆/流程类用正确率卡 90%，概念/设计类用**费曼讲解**判定——因为「很少有唯一正确答案可以字符串匹配」。多数产品会把所有知识点都塞进选择题。

**② 错误分类学**（`learning/models.py` 的 `ErrorType`）

- `structural`（知识结构）
- `deviation`（理解偏差）
- `application`（应用错误）
- `metacognitive`（元认知）

诊断出错误类型才能给对症的补救，而不是笼统「再做几道题」。

**③ 复习策略可定制但不可回溯污染**（`learning/review_strategy.py`）

四套预设（日常长期 / 考前冲刺 / 语言 / 理科），用户可自建。关键设计：

> The binding on `LearningProgress` is a **frozen snapshot** of the template, so editing or deleting a template never rewrites the scheduling of paths that already chose it.

改策略不会回溯篡改已选路径的复习计划。这是很多 SRS 产品踩过的坑（Anki 用户改设置后全库重排）。

### 2.4 用户建模：LearnerProfile 是产品的灵魂

`learning/models.py` 中 `LearnerProfile` 的注释直击要害：

> An outline used to be designed from the goal and the materials alone, so the same "I want to learn linear algebra" produced the same route for a second-year undergraduate and for a backend engineer with six evenings.

五个维度：**已会什么 / 目标是什么 / 有多少时间 / 想怎么学 / 其他**。而且——

> Intake answers are **free text on purpose**. The useful answer to "how much time do you have" is "两周，每天晚上一小时", not an enum the learner has to be translated into.

拒绝枚举、接受自然语言。表单式 intake 会劝退用户，而 LLM 恰好擅长解析自由文本。

两个精细区分：

- `TeachingPreference`：明确的教学偏好，含证据和来源 session
- `TeachingObservation`：教学尝试/困难，**明确标注「never an assessment of mastery」**

这个隔离很重要——**教学观察不能污染掌握度证据**。模型觉得「这学生好像没懂」不等于评测没过，防止 AI 的模糊印象影响硬性判定。

### 2.5 信息架构：三层空间

```
Workspace（干活）      Utility（资产/配置）        Admin（管理）
/chat  /partners       /space  /memory            /admin/users
/learning/*            /knowledge-bases
/co-writer /kanban     /notebooks /personalization
/calendar /whisper     /agents  /profile
```

全站 63 个路由。侧边栏定义在 `web/components/sidebar/nav-entries.ts`，两点值得注意：

1. **`surfaces` 字段做策略过滤**——学习账户策略生效时，没有对应 surface 的入口直接隐藏（`/settings` 例外，因为主题/语言还是他的）。
2. **导航可重排**，且注释说明了新入口的插入策略：

> Adding an entry here places it for everyone, including people who have already arranged their sidebar: it arrives next to the neighbour it follows below rather than at the bottom of their list.

新功能不会粗暴追加到用户自定义侧边栏末尾，而是插在它该在的位置。

`web/lib/capability-routes.ts` 把「哪个功能需要 LLM」声明成单一事实源，侧边栏锁图标和路由级 `CapabilityGate` 共用一份配置。多用户场景下，没有 LLM 授权的用户看到的是**加锁的入口**而非报错页。

### 2.6 Partners：把家教推进 IM

`deeptutor/partners/channels/` 有 **19 个渠道**：

```
dingtalk, discord, email, feishu, lark_http, matrix, mattermost, mochat,
msteams, napcat, qq, slack, telegram, wecom, weixin, weixin_qr, whatsapp, zulip
```

产品意图：**学习发生在用户已经待着的地方**，而不是让用户再开一个 App。
每个 Partner 有独立人设、独立库、独立技能、独立私有记忆——这是「AI 家教」从工具变成「关系」的关键一步。

### 2.7 多角色模型：Learner / Guardian / Admin

最有商业想象力的设计。管理员可创建 **Learner 账户**：

- 锁定学习能力与材料策略
- 自适应档案
- **可撤销的设备凭证（带有效期和每日限额）**
- **监护人可查看报告、审批材料、重置凭证**

明确冲着 K12 / 教育机构。技术上也做到隔离：`data/users/<uid>/` 每人独立工作区，家长和老师看到的是报告而非原始对话。

### 2.8 值得商榷的地方

1. **入口太多，认知负担重**。63 个路由，侧边栏 8 个主入口 + 5 个 utility + settings。`/whisper`、`/handoff`、`/kanban`、`/co-writer`、`/agents` 存在感很弱，容易变成僵尸功能。
2. **「四主线」概念边界对用户不清晰**。Books 和 Mastery Path 都能「学一个主题」，普通用户不知道何时该选哪个。`surfaces.ts` 的 `intro` 写得很好，但那是打开之后才看到的。
3. **能力密度过高，缺少引导式收敛**。每周加功能，但没看到「新手首次体验路径」的设计。`scripts/start_tour.py` 存在，说明团队意识到了，但优先级似乎不高。
4. **Practice 的反馈闭环偏弱**。题目自动汇入很好，但没看到「基于错题自动生成针对性微课/知识点重讲」的显式链路——而这恰是 AI 相对传统题库的核心优势。

### 2.9 总评

**这是一个「教学法优先」的产品，不是「模型能力优先」的产品。**

最聪明的地方：把教育学的硬约束（90% 才能进阶、按知识类型分流评测、间隔重复、错误分类学）写成了**不可绕过的代码策略**，而不是写在 prompt 里靠模型自觉。

`policy.py` 那句「No LLM calls, no I/O」——纯函数决策，LLM 只能提议不能判定——这是对 AI 教育产品最正确的架构选择。

最大风险是**功能广度失控**：19 个 IM 渠道、13 种语言、25 个模块、63 个路由、四种学习范式并行。对开源社区是优势，对普通用户是负担。

**产品建议优先级**：① 收敛首启体验 ② 打通「错题 → 重讲」闭环 ③ 砍掉或合并低使用率入口。

---

## 三、math_animator 故障诊断

### 3.1 问题现象

使用 math_animator 画数学动画时，出现「超长上下文」或「时间限制」报错，即使使用的是云端模型 API。

### 3.2 结论先行

**不是云端 API 的问题，是 math_animator 链路上缺少两处保护。**

这两个报错恰好都发生在最「重」的 capability 上，因为它是唯一一个**五阶段串行 + 每阶段都喂完整对话历史 + 每阶段输出巨量代码 + 重试还叠加代码**的流程。

### 3.3 上下文超长的根因

**历史被无裁剪地塞进每个阶段。**

证据链，`deeptutor/agents/math_animator/capability.py:54`：

```python
history_context = str(context.metadata.get("conversation_context_text", "") or "").strip()
```

直接进 prompt 模板（`deeptutor/agents/math_animator/prompts/zh/concept_analysis_agent.yaml:12`）：

```yaml
  {history_context}
```

**`history_context` 在这里没有任何长度上限。**

对比 chat 路径走的 `ContextBuilder`（`deeptutor/services/session/context_builder.py`）：

| 机制 | chat 路径 | math_animator 路径 |
|---|---|---|
| 历史预算 | `context_window × 0.35`，上限 `MAX_HISTORY_PLAN_TOKENS = 131072` | **无** |
| 超预算处理 | 滚动摘要 + 只保留近期原文 | **无** |
| 预算硬校验 | `_check_history_budget()` 抛 `history_budget_exceeded` | **无** |

`context_builder.py:44-50` 的注释：

> Ratio-only budgets become impractical for models with very large context windows: a 1M-token window would otherwise reserve hundreds of thousands of tokens for chat history...

**但这个预算只作用于 chat 主循环。** `conversation_context_text` 是 `ContextBuilder` 的**输出**（`build_history_text()`，`context_builder.py:219-232`，纯字符串拼接、无截断），math_animator 拿到的就是这个已经拼好的全文。

#### 为什么会累积到爆炸

`build_history_text()` 把所有非空消息用 `\n\n` 拼起来——**包括 assistant 的完整回复**。
而 math_animator 每轮输出的是：**一整份 Manim 脚本 + 完整分析 JSON + 设计 JSON**。全部进历史。

恶性循环：

```
第1轮：生成 2000 行 Manim 代码 → 存进历史
第2轮：历史里已有 2000 行代码，再生成 2000 行 → 历史 4000 行
第3轮：...
```

而且**五阶段各吃一遍**：
`concept_analysis` → `concept_design` → `code_generation` → `code_retry`（最多 5 次）→ `summary`

`concept_analysis` 吃历史；`code_generation` 吃历史 + analysis + design；`code_retry` 每次重试又吃 `current_code`（上一版完整脚本）+ `error_message`。

`deeptutor/agents/math_animator/retry_manager.py` 里 `repair_callback` 传的是 `current_code`——**每次都把上一版完整代码塞回去**，5 次重试就是 5 份完整脚本在上下文里滚动。

#### 报错为什么难懂

`ProviderContextWindowError` 在 `deeptutor/services/llm/exceptions.py:179` 定义，但全库搜索——

```
deeptutor/services/llm/image_caption_batch.py:123
```

**只有图片批处理捕获了它。** math_animator、chat loop、任何 capability 都没有针对它的降级路径。

所以它要么被 `deeptutor/services/llm/error_mapping.py:142` 的 `_message_contains("context length", "maximum context")` 分类后**直接冒泡到 UI**，要么变成一个难懂的 provider 原始错误。

**没有「历史太长，我帮你压缩一下重试」的兜底。** 这就是为什么云端大窗口模型也会撞墙——问题不在窗口多大，在于它是**无界累积**，只要对话够长，1M 窗口也会满。

### 3.4 超时的根因

两层超时，都不受用户控制。

#### ① 流空闲超时，硬编码 90 秒

`deeptutor/services/llm/provider_core/openai_compat_provider.py:1309`：

```python
idle_timeout_s = 90
...
async for event in guarded_responses(stream, progress=progress, retrieve=retrieve,
                                     owner=owner, idle_s=idle_timeout_s):
```

超时报错文案：`"Error calling LLM: stream stalled for more than 90 seconds"`。

同类硬编码：
- `anthropic_provider.py:605` → `idle_timeout_s = 90`
- `azure_openai_provider.py:214` → `idle_timeout_s = 90`

**这是硬编码常量，不是配置项。** Settings 里找不到它。

#### ② `request_timeout` 默认 60 秒

`deeptutor/services/config/provider_runtime.py:763`：

```python
request_timeout: int = 60
```

注意该字段在 `ResolvedEmbeddingConfig` 上（嵌入配置），LLM 侧实际走的是上面的 90 秒空闲判定。

#### 为什么 math_animator 特别容易撞 90 秒

因为它要求模型**一次性输出一整份 Manim 脚本**，包在 JSON 里，默认 `max_tokens = 12000`（`deeptutor/services/setup/init.py:107`）。

各 capability 预算对比：

| capability | max_tokens |
|---|---|
| math_animator | **12000** |
| video_lesson | 48000 |
| chat responding | 8000 |
| 全局默认 | 4096 |

生成 12000 token 的代码，**在流式响应下中间可能有长时间思考停顿**。如果模型是推理模型（reasoning model），会先把 token 花在思维链上。

`deeptutor/agents/math_animator/utils.py` 的 `describe_unusable_output()` 注释直接写了这个坑：

> A reasoning model that spent the whole budget on chain-of-thought fails that way every time on the same request (#1547)

推理模型思考期间**不产生输出 token**，流是静默的。90 秒静默 → 判定 stalled → 报超时。

**这就是为什么「即使使用云端模型 API」也会超时**——云端模型不是不思考，推理型云模型思考更久。

### 3.5 代码里已有的补丁，以及补不到的地方

团队已修过好几轮，issue 号都留在注释里：

| Issue | 修复 | 局限 |
|---|---|---|
| #1202 | 空/畸形 JSON 响应重试 | 只处理解析失败，不处理上下文溢出 |
| #1545 | 区分「截断 / 空 / 畸形」三种失败并报准确原因（`describe_unusable_output`） | 只改善**报错文案**，不解决根因 |
| #1547 | 截断后**提升 token 预算**重试（`escalated_max_tokens`，上限 2×） | **不降历史长度** |
| #1914 | 预算无法再涨时**提前退出**，避免确定性浪费 | 只省时间，不解决问题 |

`deeptutor/agents/math_animator/utils.py` 的 `escalated_max_tokens()` 注释很诚实：

> Retrying a truncated generation on the same budget is deterministic waste — 9 attempts, 21 minutes, same ending (#1547).

**21 分钟、9 次尝试**——这就是「超长」体感的来源。它在死磕**输出预算**，却从来没动过**输入历史**。

#1914 的注释：

> Once the budget cannot grow, another truncated call repeats the same failure instead of repairing it

预算涨到头就放弃，抛 `GeneratedCodeOutputError`。

### 3.6 现有绕过方法

按见效速度排序：

**① 开新对话再做动画**（最直接）
`history_context` 来自当前 session 历史。新会话历史为空，`(none)` 进 prompt，上下文问题立刻消失。做动画前先新开 session。

**② 调高 max_tokens，并换非推理模型**
`agents.yaml` 里 `plugins.math_animator.max_tokens`（默认 12000）可调，Settings → Capabilities 也能改。
但更关键的是**用非推理模型**——推理模型的思维链吃预算又制造静默期，是超时的双重元凶。

**③ 降低画质 / 关视觉审查**
`quality` 从 `high` 降到 `medium` 或 `low`；`enable_visual_review` 保持 `false`（默认就是 false）。
视觉审查会额外加 `review_timeout_seconds = 120` 的调用，以及最多 4 次修复重试，每次 `repair_timeout_seconds = 180`。
**最坏情况：4 × (120 + 180) = 20 分钟**（见 `retry_manager.py`）。

**④ 动画请求本身写小一点**
`parse_target_duration_seconds()` 会解析时长，但代码生成量主要取决于场景复杂度。一次只要一个动画，不要「做个视频讲解整个微积分」。

---

## 四、修复方案与优先级

### P0 — 给 `history_context` 加预算

在 `deeptutor/agents/math_animator/capability.py:54` 之后套一层截断，复用 `ContextBuilder` 的 `count_tokens` 和预算常量：

```python
history_context = str(context.metadata.get("conversation_context_text", "") or "").strip()
# 缺的就是这一层预算
history_context = truncate_to_budget(history_context, budget=...)
```

更彻底的做法：让 math_animator 走 `ContextBuilder` 而不是直接读 `conversation_context_text`。

### P1 — 重试时不要重传完整 `current_code`

`retry_manager` 的 `repair_callback(code, error_message, attempt)` 每次传全量代码。
可只传**失败位置附近的片段 + diff 提示**，或对代码做 AST 级摘要。

### P2 — 捕获 `ProviderContextWindowError` 并降级

现在全局裸奔，只有 `image_caption_batch.py` 接住了。
应在 `base_agent` 或 provider 层统一捕获，触发历史压缩后重试一次。

### P3 — 把 90 秒空闲超时变成可配置

硬编码常量对慢模型/长代码生成不友好。
至少应按 capability 区分——math_animator 需要比 chat 更长的静默容忍。

---

## 五、一句话总结

**云端 API 没问题。** math_animator 是唯一一个「五阶段串行 + 每阶段喂完整历史 + 每阶段输出巨量代码 + 重试还叠加代码」的 capability，而它恰好：

- **没有接入**项目里已经写好的上下文预算机制（`ContextBuilder`）
- **没有针对上下文溢出的降级路径**（`ProviderContextWindowError` 全库只有一处捕获）

超时则来自硬编码的 90 秒流空闲判定，而推理模型的长静默期正好踩中。

**短期最有效：新开会话 + 非推理模型 + 低画质。** 这三条能覆盖 90% 的场景。

---

*本文档由会话分析整理生成，可作为笔记本记录导入：*

```bash
deeptutor notebook add-md <notebook_id> ./DeepTutor-Review-and-MathAnimator-Diagnosis-2026-11-24.md \
  --type research --title "DeepTutor 分析与 math_animator 诊断"
```
