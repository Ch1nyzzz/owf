# owf 优化器与 RSI 层设计计划

状态: 2026-07-22 定稿(基于两天设计讨论)。本文档是优化器阶段的锚点;实现顺序见末节。

---

## 一、已定型的设计决策

### D1. 工具边界(修订版)
- **副作用原语归 harness(冻结)**: 进程/网络/文件系统的实际通道(python 沙箱、搜索 API、终端 exec)。这是安全与计费边界。
- **工具接口与组合归 workflow(可进化)**: 优化器可通过 `ctx.defineTool({name, description, schema, handler})` 定义新工具,handler 为 JS 闭包,可调 `ctx.agent`(LLM 实现的工具)或 `ctx.runTool`(组合既有原语)。→ **DSL v1.1**,必须在首轮优化 run 前落地。
- **作弊三禁区**: 逃出沙箱 / 访问评测规则外的信息源(如 realmath 联网搜答案)/ 绕过 token 计费。其余全开放。
- 环境供给(装 SageMath 二进制等)是人做的 harness 决策,进版本记录。

### D2. 优化器 = 自托管元 workflow(同构自举)
- `workflows/_meta/optimizer.js`,跑在同一个 executor 上,零架构分叉。与对象级 workflow 的差异只有配置:特权工具注册表、更大预算、可写持久态文件。
- **特权工具注册表**(harness 提供,机械强制边界):
  - `read_matrix` — iter × task × (score, tokens, 末节点状态) 聚合矩阵
  - `read_buckets` — 失败模式分桶统计
  - `read_journal(iter, task, node?)` — 按需拉取任意轨迹(runs/ 只读,**权限全开,取证按需**)
  - `read_workflow(iter)` / `diff_workflows(a, b)`
  - `write_workflow` — **事务性写入**: 静态验证(loadWorkflow 门)→ 罐头轮冒烟 → 落盘,任何一关失败退回错误
  - `run_probe` — 带预算帽的小样本评估发起
- proposer 模型可配置,首轮用强模型,后续可做全开源消融。不依赖 Claude Code/Codex CLI。

### D3. 记忆与长上下文:外置文件 + 编排,不用 compaction
- 跨轮记忆 = 文件: belief ledger、gap 池、frontier manifest。可 diff、可回滚、进 git;每次优化器调用无状态冷启动。
- 单次调用内证据过长 → 用编排解决(matrix 节点 → 按失败簇 parallel triage 节点 → synthesize 节点),不给 loop 加 compaction。**优化器自己的上下文问题必须用我们主张的编排范式解决,否则自相矛盾。**
- 留口: `_meta` 域可按需单独暴露 transformContext 截断,数据逼出来再开。

### D4. 诊断协议(双产物 + 五元标注 + 双环)
- 每轮诊断强制两个独立节点、两份产物:
  - **归因报告**(快环): 图内坐标、因果链、修复提案;实例级证据即可行动(小步:prompt/预算/接口)。
  - **缺口假设**(慢环): 反事实形式("若存在结构/能力 X,这类失败不可能")。**不直接触发动作,入 gap 池。**
- **五元标注**(每个 patch 必带): 改动轴 / 触发失败模式 / 假设 / 预测(per-task flips + token 轴)/ 实测。
  - 六轴枚举: 节点 prompt(SP)| 拓扑 | 模型路由 | 工具选用与定义 | 预算(maxTurns/token)| hooks(rails)。
- **gap 池晋级双条件**: 同簇假设 ≥3 次独立提出 + 分数矩阵弱区佐证,才触发结构编辑。
- **co-adaptation 恩赦**: 拓扑/路由类候选首评不判死,自动获得一轮参数跟进再定去留(治"结构尝试全灭→绝迹"病)。
- 统计卫生: k≥3 重复、配对比较、只认超出噪声带的 delta、修失败模式不修失败实例。

### D5. RSI 塔(三层,逐层更简单、失败代价更低)
```
对象级   workflows/{domain}/candidate.js   ← optimizer.js 优化(L0)
元级     workflows/_meta/optimizer.js      ← watchdog 修复/纠偏(L1)
元元级   workflows/_meta/watchdog.js       ← 手写、冻结、单节点,塔顶,无人再看它
```
- **watchdog 触发 = 机械谓词**(harness 从 meta-journal 计算,LLM 不参与触发):
  - 运行病理: 连续 N 轮无候选 / budget_exceeded / evidence 节点 schema_failed 率超阈 / ledger 零写入 / probe 从未发起
  - 停滞: frontier 在噪声带外无移动、连续 ≥5 轮(噪声带由 k 次重复估计)
- **停滞触发后走过程审计清单**(全部一阶可观测,判例来自 WorldCalib 实史):
  1. 编辑分布坍缩(连续 N 轮 100% 参数级,零结构尝试)← TTS 审计
  2. 重试已标 EXHAUSTED 的假设家族 ← critic 时代
  3. 追逐震荡任务(patch 瞄准重复运行间 0↔1 的题)← webshop
  4. 校准断裂(flip 预测系统性落空但 belief 不更新)← optimizer's curse
  5. gap 池失活(只进不出/从不产出缺口假设)
  6. 取证病理(从不拉 journal / 反复拉同几条)
- **三种裁决**: 清单无命中 → "健康停滞"(疑似天花板/噪声),不动优化器,升级人工;命中 → 改写 optimizer.js,改写说明必须引用具体证据;运行病理 → 修复通道。
- **护栏**: 事务写入+罐头轮 CI;git 版本化+自动回滚 last-known-good;策略改写限速(每 ≥10 轮至多一次);回滚从宽(出现新病理或候选产出下降才回滚,"还没涨分"不回滚);两臂实验中 watchdog 对称配置,每次介入=ledger 版本事件,逐 iteration 报告版本号。
- **L2 以上(watchdog 之上/优化器优化优化器以求"更强")锁死**: 二阶信号衰减,不做。

### D6. 缺口词汇来源(反本体论闭包)
- 自己的轨迹永远不含"从未存在的步骤";两个外部词汇来源:
  1. 缺口假设 prompt(反事实设计,含对**成功**轨迹的"哪里靠运气"审视)
  2. **参照轨迹 diff**: 同一 SUT 模型、无约束裸 ReAct、抽样 5-10 题,diff 出"它做而我们从不做"的行为。**必须同模型**(否则=蒸馏,污染 claim)。先手工试点一次再决定是否自动化。
- 模式库(docs/PATTERNS.md)= 静态先验版本,两者互补。

### D7. 基线诚实性纪律(realmath 0.197→0.500 的教训,升级为全域标准)
每个域的 parity 种子定稿前必须过**失败构成拆解**审查: maxTurns/预算/超时死亡占比、判分假阴性抽查。基线配置压模型 = 虚报优化增益。

---

## 二、当前状态(2026-07-22)

| 里程碑 | 状态 |
|---|---|
| M0 骨架 + DSL v1 冻结 | ✅ |
| M1 executor(12 单测 + 真实冒烟) | ✅ |
| M2 realmath 端到端 | ✅ **诚实基线 train-66 = 0.500**(130k tok/题),judge 兜底已抽查 |
| M4 bcplus | ✅ 代码;⚠ smoke 0/3 成色未查 |
| M5 finsearch | ✅ 代码;smoke 1/3,未过基线审查 |
| M3 TB2 harbor 桥接 | ⏳ 未动 |
| M6 富种子 + 模式库 | ⏳ 未动 |

---

## 三、执行顺序

### Phase A — 基建收尾(先便宜后贵)
- **A1** bcplus 0/3 排查(拉 journal 人工看:检索质量/judge/格式,分清真难 vs bug)
- **A2** M3: TB2 harbor 桥接 agent(`--agent-import-path` 薄 Python 壳 → node executor;验证 node-in-container vs host-exec 转发两种拓扑)+ **parity 校验**(与现 harness seed 0.45±0.04 打平)
- **A3** DSL v1.1: `defineTool` / `runTool` + 单测(优化 run 前的硬前置)
- **A4** finsearch / bcplus / tb2 诚实基线(每域过 D7 审查;k≥3 估噪声带)
- **A5** M6: realmath 强弱路由富种子(decompose→route→assemble)手工跑 train-66 对比 parity;TB2 冠军译文;docs/PATTERNS.md

### Phase B — 优化器 v1(冻结版)
- **B1** proposer 特权工具(D2 清单;矩阵/分桶聚合器 = WorldCalib span-builder 纪律的移植)
- **B2** `optimizer.js` v1: evidence → diagnose(双产物)→ patch(五元)→ predict → probe → ledger;gap 池文件格式
- **B3** realmath 首轮优化实验(优化器冻结,k=3,与 0.500/130k 基线配对比较)

### Phase C — RSI 层
- **C1** 健康谓词(机械)+ meta-journal 聚合
- **C2** `watchdog.js` + 罐头轮 CI + 回滚机制
- **C3** 过程审计清单 prompt(六病理 + WorldCalib 判例作 few-shot)

### Phase D — 实验矩阵(远期)
四域两臂、Pareto 前沿报告、参照轨迹试点、全开源 proposer 消融、L1 watchdog 介入案例分析。

---

## 四、每阶段验收

- A2: TB2 parity 分数 ∈ 0.45±0.04;journal token 与计费对账。
- A3: defineTool 单测覆盖(LLM-handler / runTool 组合 / journal 记账 / 禁区)。
- A4: 每域产出失败构成拆解报告 + 噪声带估计。
- B3: 首轮优化 run 产出 ≥1 个通过配对检验的候选,或诚实的 EXHAUSTED 结论;全程 meta-journal 可审计。
- C2: 注入故障(改坏 optimizer.js)→ watchdog 修复 → 回滚路径演练通过。
