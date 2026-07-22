# owf 优化器与 RSI 层设计(v2,第一性原理版)

状态: 2026-07-22 重写。v1 的机制堆(双产物节点/gap 池引擎/五元 schema/恩赦规则/参照轨迹)已撤销,
理由见 §四;git 历史保留 v1 供追溯。

---

## 一、问题的第一性分解

本质问题:**用一个 LLM agent,对一个程序(workflow.js),在昂贵且有噪声的评估下做搜索。**

不可约组件只有四个:

1. **可信的测量** — k 次重复、配对比较、噪声带估计。没有它,一切增益都是幻觉。
2. **完整的证据访问** — agent 能随机访问任何历史轨迹、分数、diff。
3. **编辑与试错能力** — 能改 workflow 文件、能发起带预算帽的小样本评估。
4. **跨轮记忆** — 一份 agent 自己维护的笔记,否则每轮从零开始。

其余一切都是"如何当一个好优化器"的**经验建议**,归属在 skill 文本里,不建成系统机制。

## 二、硬机制(harness 强制 —— 只因 prompt 保证不了)

| 机制 | 实现 | 状态 |
|---|---|---|
| 测量 | runner 的 k 重复 + 配对 + 噪声带 | 已有(k 默认待升 3) |
| 写入门 | `write_workflow` 事务:静态验证(loadWorkflow)→ 罐头轮冒烟 → 落盘;坏文件落不了地 | 待建 |
| 边界 | 特权工具注册表只开 workflows/ 写、runs/ 读;优化器机械上碰不到 harness 代码 | 待建 |
| 预算 | 每轮优化 token/墙钟硬顶 | 已有(executor budget) |
| 版本化 | 每次改动 = git commit;回滚 = revert | 已有(git) |

## 三、优化器本体:一个自由的强 agent

`workflows/_meta/optimizer.js` —— 同一 executor 上的普通 workflow,起步可以就是**单个强模型节点**
(证据太长时才用编排分诊:按失败簇并行细读 → 汇总节点只读结论,即 triage→synthesize)。

特权工具:
- `read_matrix` — iter × task × (score, tokens, 末节点状态) 聚合
- `read_buckets` — 失败模式分桶统计
- `read_journal(iter, task, node?)` — 任意轨迹按需拉取(权限全开,取证按需)
- `read_workflow(iter)` / `diff_workflows(a, b)`
- `write_workflow` / `run_probe`(带预算帽)
- `notes` — 读写自己的 NOTES.md(格式自定,agent 自己进化自己的记法)

跨轮记忆 = NOTES.md 等外置文件(可 diff、可回滚、进 git),每轮无状态冷启动读回。
**不用 compaction/session**:优化器自己的长上下文问题必须用编排解决,否则与项目主张自相矛盾。

## 四、Skill 忠告(原则 + 判例,不是规则引擎)

总纲:**凡是能用"去查证据"回答的问题,不要用"定规矩"回答。**(v1 的 gap 池计数、恩赦轮数
等配额机制,全部是证据受限假设下的发明;我们的证据访问全开,配额退化为查证纪律。)

1. **改前立假设与预测,改后对账。** 预测到 per-task 粒度(哪几题会翻、token 变化多少);
   实测与预测的偏差是唯一的学习信号,预测老落空说明对失败原因的理解是错的。
   (= WorldCalib per-task-flip 协议,已验证。)
2. **失败的两个方向都要想**:组件坏了(归因:哪个节点干砸了)与组件缺了(缺口:什么节点的
   存在会让这类失败不可能)。归因证据便宜,不自觉就会挤掉缺口思考——两个方向都写进笔记。
3. **结构假设:动手前先回查历史。** 一条轨迹引出的结构想法,立刻去检索全部历史轨迹验证
   是否成类;历史答不了的才花小样本探针。读轨迹免费,优化轮昂贵——先穷尽回溯验证。
4. **结构候选按机制层评价,不只看总分。** 新节点自己的子目标达成了吗?(拆解节点拆得好不好,
   与总分是两个问题。)机制成立而整合不佳 → 留着修整合;机制本身失败 → 弃并记档防重试。
   (= WorldCalib mechanism-effect 协议的推广:Behavioral check 与 Aggregate check 分开判。)
5. **统计卫生**:单次失败是弱证据;修失败模式,不修失败实例;只认噪声带外的 delta;
   震荡任务(重复运行 0↔1)不作为 patch 依据。
6. **已知堕落模式判例**(全部来自 WorldCalib 实史,写进 skill 当反面教材):
   编辑分布坍缩成纯 prompt 抛光(TTS 审计)/ 重试已否证假设(critic 时代)/
   追逐噪声任务(webshop)/ 预测落空却不更新信念(optimizer's curse)/
   只看聚合从不细读轨迹(evidence 审计)。

工具边界(维持 v1 的 D1):副作用原语归 harness 冻结;工具接口与组合可由 workflow 通过
`defineTool`/`runTool` 自定义(DSL v1.1);作弊三禁区 = 逃沙箱 / 评测规则外信息源 / 绕计费。

~~参照轨迹~~:已删。缺口思考由优化器自己的反事实推理承担(忠告 2),无需外部轨迹提词。

## 五、Watchdog(元级看门狗,瘦身版)

`workflows/_meta/watchdog.js` —— 手写、冻结、单节点,RSI 塔顶,无人再看它。

- **触发 = 机械谓词**(harness 从 meta-journal 计算,LLM 不参与触发):
  连续 N 轮无候选 / 优化轮 budget_exceeded / ledger·笔记零写入 / probe 从未发起 /
  frontier 在噪声带外无移动连续 ≥5 轮(停滞)。
- **触发后**:watchdog 读 optimizer 全历史(meta-journal + NOTES.md + optimizer.js 源码,
  量小,单节点装得下),判断三选一:
  a. **健康停滞**(疑似天花板/噪声,过程无病理)→ 不动,升级人工;
  b. **过程病理**(对照忠告 6 的判例清单,须引用具体证据)→ 改写 optimizer.js;
  c. **运行故障**(爆上下文/不出结果/取证死循环)→ 修复。
- **护栏**:改写走同一事务写入门;git 版本化 + 回滚 last-known-good;
  病理判定必须引证(ledger 行、编辑分布统计),"感觉换个思路更好"不构成理由;
  两臂实验中 watchdog 对称配置,介入 = 版本事件,逐 iteration 报告版本号。
- **L2 以上锁死**:不做"让优化器更强"的二阶优化(信号衰减),watchdog 只管"活着且不堕落"。

## 六、基线诚实性纪律(全域 gate)

每个域的 parity 种子定稿前必须过**失败构成拆解**:maxTurns/预算/超时死亡占比、判分假阴性抽查。
基线配置压模型 = 虚报优化增益。(realmath 教训:maxTurns 20→64 + judge 兜底,0.197→0.500。)

---

## 七、当前状态(2026-07-22)

| 里程碑 | 状态 |
|---|---|
| M0 骨架 + DSL v1 冻结 | ✅ |
| M1 executor(12 单测 + 真实冒烟) | ✅ |
| M2 realmath 端到端 | ✅ 诚实基线 train-66 = **0.500**(130k tok/题) |
| M4 bcplus | ✅ 代码;⚠ smoke 0/3 成色未查 |
| M5 finsearch | ✅ 代码;smoke 1/3,未过基线审查 |
| M3 TB2 harbor 桥接 | ⏳ |
| M6 富种子 + 模式库 | ⏳ |

## 八、执行顺序

### Phase A — 基建收尾
A1 bcplus 0/3 排查 → A2 M3 TB2 桥接 + parity 校验(0.45±0.04)→ A3 DSL v1.1(defineTool/runTool + 单测)
→ A4 各域诚实基线(过 §六 gate,k≥3 估噪声带)→ A5 M6 富种子(realmath 强弱路由 decompose-route
手工对比;TB2 冠军译文;docs/PATTERNS.md)

### Phase B — 优化器 v1
B1 特权工具(§三清单;矩阵/分桶聚合器)→ B2 optimizer.js v1(单强节点起步)+ skill(§四忠告全文)
→ B3 realmath 首轮优化实验(优化器冻结,k=3,与 0.500/130k 配对比较)

### Phase C — Watchdog
C1 机械健康谓词 + meta-journal 聚合 → C2 watchdog.js + 事务写入 CI + 回滚演练
(注入坏 optimizer.js → 修复 → 回滚,全路径过一遍)

### Phase D — 实验矩阵(远期)
四域两臂、(score, tokens) Pareto 报告、全开源 proposer 消融、watchdog 介入案例分析。

## 九、验收

- A2: TB2 parity ∈ 0.45±0.04;journal token 与计费对账。
- A3: defineTool 单测(LLM-handler / runTool 组合 / 记账 / 禁区)。
- A4: 每域失败构成拆解报告 + 噪声带数字。
- B3: ≥1 个过配对检验的候选,或诚实 EXHAUSTED;meta-journal 全程可审计;
  笔记里可见"假设→预测→对账"闭环。
- C2: 故障注入演练通过。
