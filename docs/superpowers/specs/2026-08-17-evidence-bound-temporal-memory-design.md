# 证据约束的时态经验记忆设计

日期：2026-08-17

状态：研究方案；第一阶段基础设施已实现

## 目标

为 Coding Agent 增加有用的跨会话记忆，但不能把原始聊天、工具输出或模型生成的摘要
变成可信指令。记忆层应帮助 Agent 找回项目决策、避免重复失败并复用已验证流程，
同时保留 Runtime 现有的事务、验证、隐私和防篡改边界。

## 当前基础

Runtime 已经有三种持久化机制，但都不是长期记忆系统：

- `chat.py` 保存有界消息列表，并用于恢复单次会话；
- `context.py` 把较早消息压缩为有损的 human-role 摘要；
- trajectory 和 receipt 保存可审计的执行与验证证据。

这些机制提供了较强的情景证据，但召回能力较弱。事实、决策、约束、失败假设和可复用流程
仍混在消息文本里，缺少跨会话索引、时态失效、冲突模型、来源感知检索和经验归纳。

## 前沿研究归纳

有价值的方向不是单纯扩大聊天记录，也不是只加一个向量数据库。较强的系统通常会分离
不可变证据、派生记忆、检索线索和记忆管理策略。

- [Graphiti/Zep](https://github.com/getzep/graphiti)
  （[论文](https://arxiv.org/abs/2501.13956)）把 episode 保留为来源证据，
  为事实设置有效期，保留已失效历史，并融合语义、关键词和图检索。这是时态更新与冲突处理
  最值得参考的方案。
- [Memora](https://github.com/microsoft/Memora)
  （[论文](https://arxiv.org/abs/2602.03315)）把完整记忆值、主要抽象和多个 cue anchor
  分开。这样既能保留细节，又能防止原始内容主导检索。
- [A-MEM](https://github.com/agiresearch/A-mem)
  （[论文](https://arxiv.org/abs/2502.12110)）把记忆设计成类似卡片盒笔记法的动态连接节点。
  连接思路很有价值，但静默原地修改不适合审计优先的 Runtime。
- [ReasoningBank](https://github.com/google-research/reasoning-bank)
  （[论文](https://arxiv.org/abs/2509.25140)）从成功和失败轨迹中提炼可复用推理策略，
  很适合已验证 Coding Run 和重复调试失败的场景。
- [Mem0](https://github.com/mem0ai/mem0)
  （[论文](https://arxiv.org/abs/2504.19413)）展示了面向生产的抽取和多信号检索路径，
  其近期方案更倾向追加式记忆、实体连接、BM25 与语义融合及时间排序。
- [LangMem](https://github.com/langchain-ai/langmem) 提供了有用的 LangGraph 集成模式：
  显式热路径工具，以及后台抽取和归并流程。这里把它作为集成参考，而不是必需依赖。
- [MemOS](https://github.com/MemTensor/MemOS)
  （[论文](https://arxiv.org/abs/2507.03724)）把记忆提升为带来源和版本化 `MemCube`
  的受管资源，但它的整体范围对本项目而言过大。
- [MemSkill](https://github.com/ViktorAxelsen/MemSkill)
  （[论文](https://arxiv.org/abs/2602.02474)）把抽取、归并和裁剪策略视为可演化的元记忆技能。
  这适合作为后期方向，前提是项目已积累足够多经过评测的记忆轨迹。
- 关于[来源约束记忆权限](https://arxiv.org/abs/2606.24322)的近期研究说明：
  只根据内容判断信任度，以及使用可变 lineage，都不足以抵御 memory laundering。
  权限必须在写入时绑定，并且不能在摘要或工具回显过程中提高。

2026 年关于[存储、反思与经验](https://arxiv.org/abs/2605.06716)的综述框架
也与本仓库契合：trajectory 已经覆盖存储，下一步应是证据约束的反思和跨轨迹经验抽象，
而不是保存更多原始历史。

## 总体架构

引入 **证据约束的时态经验图（Evidence-Bound Temporal Experience Graph，ETEG）**，
分为三个平面。

### 证据平面

证据平面保持追加写入，并作为事实来源：

- 聊天轮次和完整工具边界；
- 事务 trajectory 与 receipt；
- 工作区 fingerprint、commit、branch 和引用文件 hash；
- 测试/检查结果与提交状态；
- 事件写入时分配的不可变来源标签。

现有私有 trajectory 存储仍是权威证据。记忆数据库只保存引用和摘要 hash，
不重复存储可能包含源码或敏感内容的完整工具输出。

### 知识平面

派生知识保存为版本化记忆卡片：

- **语义记忆（semantic）：**事实、决策、约束、偏好、约定、修正；
- **情景记忆（episodic）：**压缩后的任务结果和重要事件；
- **程序记忆（procedural）：**已验证策略、工作流、工具特性和失败预防；
- **状态记忆（state）：**当前目标、未解决阻塞和待跟进事项。

每张卡片包含高保真值、一个主要抽象、多个 cue anchor、类型化 scope、时态有效期、
来源、权限和证据引用。卡片通过 `derived_from`、`supports`、`supersedes`、
`contradicts`、`related_to` 等类型化边连接。

卡片不会被静默改写。修正会创建新卡片和 `supersedes` 或 `contradicts` 边；
旧卡片保留用于历史查询，但默认当前状态检索会排除它。

### 控制平面

控制平面决定抽取、提升、检索、归并、老化和 tombstone。第一版使用确定性策略加结构化
模型输出，有意推迟学习型记忆技能。

控制平面应隐藏在小型协议之后，使未来的 LangMem、Mem0、Graphiti 或学习型策略适配器
可以替换实现，而不会渗入核心 Runtime。

## 记忆卡片模型

一张卡片至少应包含：

```text
id
kind                         semantic | episodic | procedural | state
subtype                      decision | constraint | failure | strategy | ...
scope                        global | repo | branch | file | symbol | test | tool
scope_key
value                        高保真记忆值，不直接建立索引
abstraction                  用于索引的规范摘要
cue_anchors[]                语义或词法检索入口
status                       active | superseded | disputed | stale | tombstoned
valid_from / valid_to        该结论在项目中的有效时间
recorded_at                  Runtime 获得该信息的时间
origin                       user | trusted_tool | agent | external
authority                    act | inform | none
confidence / importance
source_refs[]                trajectory/event/receipt/commit/file-hash 引用
content_sha256 / provenance_hmac
```

第一版使用现有私有状态目录下的 SQLite：

- 普通表保存卡片、来源、类型化边和状态事件；
- FTS5 只索引主要抽象和 cue anchor；
- JSON 仅用于不需要关系查询的有界属性；
- 初始采用 `synchronous=FULL` 的 rollback journal；引入多进程并发写入后再评估 WAL；
- 数据库权限为 `0600`，并复用现有状态目录保护。

核心功能不依赖图数据库或向量数据库。后续可以把 embedding 作为可选能力；标准库
SQLite/BM25、scope 过滤和浅层图扩展构成确定性的离线基线。

## 权限与投毒边界

记忆是上下文，不是策略。

- 召回内容以明确分隔的 human-role 建议块注入，绝不能作为指令追加到 system prompt。
- `origin` 和 `authority` 在写入时确定，并纳入记忆 HMAC。归并和摘要只能保持或降低权限。
- 原始仓库文本、网页、工具输出和模型结论默认是 `none` 或 `inform`；它们可以引导检查，
  但不能授权有后果的操作。
- 用户陈述和可信 Runtime 证据可以携带更高权限，但只有当前轮次的新鲜用户意图或既有
  Runtime 策略能够真正授权操作。
- 已验证 receipt 可以提升与验证结果直接相关的程序经验，但不能顺带提升同一 trajectory
  中的无关结论。
- 每条召回卡片都包含紧凑来源和状态。默认排除有争议、已过期和被替代的卡片。
- 删除使用经过认证的 tombstone，不会静默重写证据账本。
- FTS 等加速索引不具备独立信任权；结果必须重新与已签名内容核对。

这样可以避免记忆投毒演变为延迟触发的验证或审批绕过。

## 写入生命周期

抽取应由高信号边界触发，而不是每个 token 或工具调用都触发。

1. 正常保存 checkpoint 或事务 trajectory。
2. 从脱敏事件、receipt 证据、任务结果、diff 元数据、检查结果和来源引用构造有界输入。
3. 按严格 schema 生成候选卡片，并在写入时分配来源标签。
4. 应用确定性准入规则：
   - 已验证提交可以创建已验证程序记忆候选；
   - 失败检查可以创建失败预防候选；
   - 未验证的 Agent 结论只保留建议权限；
   - 拒绝秘密、完整源码正文和超大工具输出。
5. 以追加方式写入卡片和类型化边。
6. 后续出现冲突证据时，创建新 revision 并关闭旧卡片的当前状态。

Chat 抽取应发生在显式关闭、空闲/防抖或用户主动 `remember` 的边界。
事务抽取应发生在 prepare/submit 之后，因为此时验证证据最强。

## 跨轨迹经验归并

只保存原始任务摘要是不够的。周期性归并器应比较多条相关结果：

- 同一错误特征对应的成功与失败尝试；
- 反复影响同一 symbol 或子系统的决策；
- 跨仓库或分支复用的流程；
- 因文件、commit 或依赖变化而失效的记忆。

归并器创建新的派生策略卡片，并明确记录支持和反对来源。提升需要满足最低证据要求，
且不能删除被归纳的 episode。这相当于把 ReasoningBank 的成功/失败学习方法，
落到本 Runtime 的已验证事务证据上。

## 检索与上下文打包

检索采用有界流水线：

1. 把意图分类为 `locate_code`、`debug_failure`、`decision_history`、`procedure`、
   `project_state` 或 `general`。
2. 应用 repo/branch/file/symbol/test/tool scope 过滤。
3. 通过 abstraction 和 cue anchor 上的 FTS/BM25 召回候选。
4. 配置 embedding provider 时，可选择融合语义分数。
5. 类型化图最多扩展一到两跳。
6. 按相关性、scope 距离、时态有效性、证据强度、验证结果、新近度和历史效用排序；
   对冲突、过期和重复内容降权。
7. 使用类似 MMR 的去重与多样化方法，生成小型、带引用的上下文包。

上下文包应分区呈现当前决策与约束、相关已验证流程、已知失败路径以及历史或争议上下文。
每项都包含 ID、状态、来源、权限和紧凑证据引用。需要时，模型可以通过只读记忆工具
请求完整卡片或支撑 episode。

## Runtime 集成

应新增职责单一的模块，而不是继续扩张 `chat.py` 或 `agent.py`：

- `memory_contracts.py`：store、extractor、consolidator、retriever 协议；
- `memory_models.py`：不可变卡片、边、来源和上下文包；
- `memory_store.py`：私有 SQLite 实现与迁移；
- `memory_admission.py`：解析 Runtime 证据并独占分配来源、权限和作用域；
- `memory_policy.py`：准入、权限、老化和提升规则；
- `memory_retrieval.py`：查询规划、融合、图扩展和打包；
- `memory_extraction.py`：从有界证据进行结构化抽取；
- `memory_service.py`：在 Chat 和事务生命周期边界编排。

第一阶段只读 CLI：

```text
mca memory status
mca memory search QUERY
mca memory show ID
mca memory sources ID
mca memory verify
```

读路径稳定后再增加写入和治理命令：

```text
mca memory remember TEXT
mca memory correct ID TEXT
mca memory forget ID
mca memory consolidate
```

为保持兼容，功能默认关闭。事务运行可通过 `--memory local` 显式启用私有 SQLite 后端；
只有后续成功 commit 才会形成程序记忆。
即使没有 embedding 模型、网络服务或新增依赖，Runtime 也必须继续可用。

## 交付阶段

### Phase 0：契约与评测样例

- 定义 schema、来源/权限规则和威胁模型测试。
- 增加时态更新、冲突、过期代码、重复失败、流程复用、拒绝臆造和投毒样例。
- 暂不写入生产记忆。

### Phase 1：确定性本地记忆

- 实现私有 SQLite store、FTS5 cue、类型化边、HMAC 校验和只读 CLI。
- 从已完成 trajectory 和 receipt 确定性生成情景卡片。
- 在显式启用开关后增加有界检索和上下文包渲染。

当前已经完成本阶段的存储、安全、时态替代、检索和只读 CLI 基础。第一条 Runtime 所有的
准入路径已经覆盖经过认证的事务 receipt：它会重新绑定持久化 manifest/trajectory，并只生成
`inform` 级程序记忆。事务显式设置 `--memory local` 后，成功 commit 会确定性保存经过命令
指纹绑定的验证流程；自由文本模型抽取、其他来源解析器和上下文注入仍保持关闭。

### Phase 2：语义与程序抽取

- 增加与 provider 无关的结构化抽取。
- 在完整生命周期边界提取决策、约束、失败和已验证流程。
- 增加 revision、替代、时态有效期以及文件/commit 过期检测。

### Phase 3：跨轨迹经验

- 把已验证成功和失败归并为可复用策略。
- 在不改变权限的前提下跟踪检索效用和结果统计。
- 提供可选本地 embedding 和分数融合。

### Phase 4：自适应元记忆

- 只有积累足够多带标签的失败和检索结果后，才评测学习型查询路由与记忆技能。
- 学习策略始终保持建议性和版本化；确定性权限与验证规则留在学习控制器之外。

## 评测

主要 benchmark 应针对 Coding Agent，而不能只依赖对话式问答。

- **召回：**固定上下文预算下的决策、约束、错误特征、symbol 和已验证流程召回。
- **Agent 结果：**任务成功率、验证率、重复失败率、交互步数、延迟和 token 使用量。
- **时态正确性：**当前与历史决策，以及依赖/文件漂移后的判断。
- **拒绝臆造：**缺少证据时拒绝编造记忆。
- **安全：**直接投毒、摘要洗白、可信工具回显、伪造佐证、权限提升、HMAC 篡改、
  跨仓库泄漏和 tombstone 完整性。

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) 和 LoCoMo 可验证通用记忆行为；
仓库原生 benchmark 则应复用本项目的确定性评测风格和已验证事务结果。

## 暂不采用的第一步

- **把每条消息都写入向量数据库：**噪声大、成本高、因果与时态更新能力弱，
  也不适合作为权限来源。
- **直接采用 Neo4j/Graphiti：**有参考价值，但对核心包及其离线保证而言运维成本过高。
- **把 LangMem 加为必需依赖：**集成模式很好，但本 Runtime 需要更小、可感知信任的契约，
  并保持 provider 与存储独立。
- **让模型自由修改 `MEMORY.md`：**虽然可检查，但缺少原子来源、冲突历史、scope 隔离
  和可检测篡改的权限。
- **立即训练记忆控制器：**目前还没有足够的项目特定反馈数据，无法证明成本和信任风险合理。

## 第一阶段验收标准

- 默认 Runtime 行为和依赖集合不变。
- 记忆保持私有、有界、可检查并能独立验证。
- 任何记忆变换都不能提升权限。
- 每条召回结果都有来源引用和状态标签。
- 无需 embedding，系统也能通过 SQLite/FTS 离线工作。
- 被替代事实仍可用于历史查询，但不会进入默认当前状态召回。
- 已验证流程可以追溯到具体 receipt 和工作区状态。
- 被投毒或篡改的记忆不能授权编辑、命令、提交或其他有后果的操作。
