# 证据约束的通用记忆（显式启用）

记忆基础设施默认与 Agent Runtime 隔离。`run`、`chat` 和默认的 `tx` 不会读取或写入记忆，
也没有新增必需依赖。事务显式使用 `--memory local` 时会读取同一 workspace 的记忆，并在成功
commit 后形成确定性记忆；交互聊天显式使用 `mca chat --memory local` 时会记录来源事件，并只
通过用户确认命令写入 durable 记忆。
这样可以让第一阶段保持可逆，并确保默认行为不变。

## 存储与信任模型

本地记忆位于现有私有状态目录的 `memory/` 子目录。平台的 SQLite 支持 FTS5
时使用全文索引，否则自动退回确定性的 `LIKE` 检索。

- 记忆卡片是不可变的，类型包括语义记忆、情景记忆、程序记忆和状态记忆。
- 状态变化记录为追加式认证事件。被替代、存在争议、已过期或已删除的卡片仍可审计，
  但默认检索不会返回它们。
- 证据引用和类型化图关系单独存储并签名。
- 卡片内容、证据、关系、状态事件以及 schema 元数据均使用 HMAC-SHA256 认证。
- 外部内容的权限只能是 `none`；Agent 和可信工具产生的内容最高只能是建议级
  `inform`。只有直接来自用户的记录才能携带 `act`，派生记录不能高于最弱来源的权限。
- FTS 只负责加速候选召回。返回结果前，系统会再次使用已认证的摘要和 cue 文本核验，
  防止未签名索引把无关内容伪装成命中结果。
- 每张可检索卡片都必须至少绑定一条证据来源。
- 写入由跨线程/跨进程锁和 SQLite 立即写事务串行化；同一 receipt 或同一来源的并发重放
  只会保留一个逻辑记录。

在 POSIX 系统上，数据库目录权限为 `0700`，数据库和密钥权限为 `0600`。
系统会拒绝符号链接、非当前用户所有或权限过宽的状态路径。

## 检查与离线管理命令

```bash
mca memory status
mca memory search "pytest 验证"
mca memory search "旧决策" --all-statuses --limit 20
mca memory show MEMORY_ID
mca memory sources MEMORY_ID
mca memory verify
mca memory health
mca memory list --cwd /path/to/repo
mca memory forget ID_OR_QUERY --cwd /path/to/repo
mca memory correct ID_OR_QUERY "NEW VALUE" --cwd /path/to/repo
mca memory candidates list --cwd /path/to/repo
mca memory candidates approve CANDIDATE_ID --scope user --cwd /path/to/repo
mca memory candidates dismiss CANDIDATE_ID --cwd /path/to/repo
mca memory backup /private/path/memory-backup.zip
mca memory purge --yes
mca memory restore /private/path/memory-backup.zip
```

前六个原有检查命令不会初始化数据库，也不会写入记录。`list` 也只读；`forget`、`correct` 和
`candidates approve/dismiss` 是显式的离线用户变更。`backup` 在导出前验证 SQLite、对话链和跨存储
证据，生成带文件摘要清单的私有 ZIP；它是明文敏感数据，不是加密备份。`restore` 只恢复到不存在
的记忆目录，并在安装前重新验证；`purge --yes` 不可逆地删除整套本地记忆、密钥和证据，但不会
删除外部备份。普通 `run`、默认聊天和默认事务仍不接触记忆。

`mca memory verify` 会检查 SQLite 完整性、所有记录的 HMAC、引用关系、状态事件、
证据覆盖、FTS 索引、对话/候选 HMAC 链，以及候选和卡片到原始事件的证据绑定。校验失败应视为硬错误：在根据原始证据修复或重建前，
不得继续使用该记忆库。

`mca memory health` 在完整性校验之外，还报告活跃/非活跃卡片、已经到期但状态仍为 active
的卡片、尚未生效的卡片、作用域数量和数据库大小。这些指标用于发现长期运行后的生命周期
债务，不会自动删除或改写记忆。

## 当前边界

当前阶段已经提供：数据 schema、时态替代、权限约束、认证证据、本地词法检索、只读检查，
以及可独立调用的“通用内核 + 场景策略”检索层。检索层包含：

- 明确的 `scope/scope_key` 作用域匹配；
- 当前状态和 `valid_from/valid_to` 有效时间过滤；
- 词法 BM25、精确 cue、cue 重叠和认证关系图候选；
- RRF 多路融合、来源/权限约束、候选分差门控；
- 明确、可审计的 `use_memory` 或 `no_memory` 决策；
- 编码、研究、个人助理、客服的策略预设，且共用同一个存储和检索实现。
- 可选的 `SemanticCandidateProvider` 接口；embedding/reranker 只能看到已经通过硬过滤的候选，
  默认不配置，不新增模型或网络依赖。

`mca tx run --memory local` 在开始时直接调用原证据时态检索器，只读取同一 workspace、当前
有效且至少具有 `inform` 权限的卡片，并把带来源的有限上下文包注入 Agent。成功 commit 后，
Runtime 从认证 receipt 形成验证流程程序记忆，并从 receipt 绑定的 `prepared.patch` 自动形成
一条 `verified_repair` 情景经验；默认仍为 `off`。二进制、超过 200 KB 或疑似包含凭证的补丁
不会写入经验正文。聊天路径只接受 `/remember` 或 `/remember @候选ID` 的显式准入；偏好式
自由文本最多暂存为待审批候选，不会自动成为 active 卡片。默认没有 embedding，更不会让
记忆授权 Agent 执行操作。

## 交互聊天长期记忆

```bash
mca chat --cwd /path/to/repo --model deepseek --memory local
```

启用后可使用：

```text
/remember TEXT          明确保存一条当前 workspace 记忆
/remember --scope user TEXT       保存跨 workspace 的本地用户记忆
/remember --scope workspace TEXT  明确保存当前 workspace 记忆
/correct MEMORY_ID TEXT 用新 revision 替代旧记忆
/forget ID_OR_QUERY     tombstone 唯一命中的记忆
/memory [QUERY]         查看本地用户 + 当前 workspace 的活跃记忆
/memory candidates      查看自动暂存、尚未批准的候选
/remember @ID           按候选默认作用域批准（启发式候选默认为 user）
/remember --scope workspace @ID   以当前 workspace 作用域批准候选
/memory dismiss ID      放弃一个候选
```

每个用户/助手事件都会追加到私有状态目录下的 HMAC 链式 JSONL 日志。每行绑定日志名、连续序号、
前一行 HMAC 和完整 payload；候选及审批决定使用另一条同样认证的链。有效的旧版自摘要日志首次
打开时会在锁内原子迁移。活跃记忆仍写入原有
HMAC 认证 SQLite，并绑定原始事件 reference 与 digest；修正保留 supersede 边，遗忘保留
tombstone 和用户遗忘事件证据。明显凭证不会进入 durable 卡片。原始聊天事件和 trajectory
本身仍可能敏感，HMAC 只检测篡改而不加密，必须按私有状态处理。

每轮召回最多 4 条、总计 5,000 字符，只读取稳定的本地 user 作用域和当前 workspace 作用域。
注入块明确标记为带来源但可能过时/错误的历史数据，不能升级 `/ask`、`/code`、shell、写文件或
验证权限。`/clear` 只清空当前模型上下文；若要移除长期记忆必须使用 `/forget`。

聊天也可传入 `--embedding-base-url` 与 `--embedding-model`，并用
`--embedding-api-key-env`/`--embedding-timeout` 配置可选的 OpenAI-compatible 语义候选路由。
该路由只接收已经通过作用域、权限、时态和证据硬过滤的候选；失败时退回词法检索。远程 endpoint
仍会看到查询和有限候选正文，敏感数据应使用本地/内网服务或保持关闭。

检索会先在 SQLite 中把候选缩到请求作用域和 `global`，再验签每张返回卡及其最新状态事件，
因此其他项目的历史不会参与当前项目的排序。生产入口在此之前仍执行全库完整性校验；这项
优化没有把未签名索引升级为证据，也没有为速度跳过 HMAC、引用或 FTS 一致性检查。

生产上下文使用独立于 Agent 的硬预算：总计最多 16,000 字符、单条最多 6,000 字符、最多
3 条。超长补丁按任务词、diff 文件头和 hunk 附近提取片段并标记截断，因此不会触发 Agent
20,000 字符的 advisory 上限。结构化检索审计只记录决策、原因、被选内容 SHA-256、字符数和
是否截断，不复制记忆正文。为了可靠 resume，私有 trajectory 的首条 HumanMessage 仍会包含
实际注入的有限上下文；它受现有 `0600` 状态文件、redactor 和上下文预算保护。

项目身份保存在 Git local config 的 `mca.memoryIdentity`，由随机 UUID 派生为 SHA-256；移动本地
checkout 后仍能命中原作用域，而且不会改动 tracked 文件。重新 clone 不会复制 local config，
会被视为新项目；需要跨 clone 迁移时应显式导入身份，而不是依赖 remote URL 自动合并记忆。

每个作用域默认最多保留 64 条 active repair，正文合计不超过 1,000,000 字符。超过容量时最旧
经验追加 `stale` 事件而不是物理删除，仍可审计但不再默认召回。凭证检查除常见 provider token、
私钥和敏感字段赋值外，还覆盖敏感行中的高熵值；环境变量引用不会被当作真实凭证。

检索器之后现有一个实验性的结果感知控制层：它根据运行阶段、可信结果反馈、反例和 token
成本选择检索、警告、重查或克制，并支持不注入、不学习的影子策略。设计、运行方式和边界见
[结果感知的记忆控制层](memory-control.md)。自然经验迁移实测中该层稳定落后于原检索器，因此
它只保留为独立实验组件，没有接入生产 Agent 上下文，也不是训练出的策略。

## 通用核心与 MCA 适配器

`src/memory_core/` 不 import `mini_code_agent`，只定义证据、经验、项目身份、存储、语义候选、
上下文投递协议，以及确定性形成、预算、安全和生命周期策略。`MemoryRuntime` 通过构造函数接收
宿主实现，不知道 transaction、Git、CLI 或具体 Agent。

MCA 专用逻辑位于 `src/mini_code_agent/memory_adapters/`：

- `transaction.py` 把认证 transaction receipt 与 prepared patch 转成 `VerifiedExperience`；
- `project.py` 提供 Git-local 稳定项目身份；
- `agent.py` 把原检索结果转成有限上下文和无正文审计。

离线 portability 套件使用普通 CI receipt、独立身份提供器和内存仓库跑通证据→形成→存储→
检索→渲染，不需要 MCA transaction 或 Agent：

```bash
.venv/bin/python -m evals.run_memory_portability
```

这证明核心接口可被其他宿主使用；当前 HMAC SQLite 实现仍由 MCA 包提供，若要发布独立 PyPI
库，下一步可再把该实现移动到单独 backend，而不改变核心协议。

## 可选 Embedding Backend

不要求一定在本机部署模型。`memory_core.semantic` 提供三层接口：宿主可直接实现
`EmbeddingClient`，也可以使用内置的 OpenAI-compatible HTTP 客户端；HTTP 地址既可以是远程
托管服务，也可以是本机或内网推理服务。MCA 默认完全关闭该路由，只有新事务显式提供模型和
endpoint 时才启用：

```bash
export MCA_EMBEDDING_API_KEY='...'  # 本地无鉴权服务可以省略
mca tx run "Fix the parser regression" \
  --cwd /path/to/repo \
  --model YOUR_CODING_MODEL \
  --memory local \
  --embedding-base-url https://embedding.example/v1 \
  --embedding-model YOUR_EMBEDDING_MODEL \
  --test-command "pytest -q" \
  --yes
```

也可使用 `MCA_EMBEDDING_BASE_URL` 和 `MCA_EMBEDDING_MODEL`，API key 的环境变量名可通过
`--embedding-api-key-env` 指定。endpoint 必须实现 `POST {base_url}/embeddings`，输入输出遵循
OpenAI-compatible embedding JSON。

安全和运行边界：

- embedding 只接收已经通过 scope、状态、时间、权限和证据过滤的候选，但远程 endpoint 仍会
  收到查询和这些候选的有限正文；代码不能外发时应使用本地/内网服务或保持关闭。
- 每个输入最多 12,000 字符；向量必须维度一致且全为有限数值。
- 派生向量按 endpoint+model namespace 和文本 SHA-256 缓存在私有
  `memory/embedding-cache.sqlite3`，不会存 API key，也不作为信任证据。
- endpoint 超时、缓存损坏或返回非法向量时，语义路由 fail-open 为空并退回原词法/图检索；
  trajectory 审计记录 `semantic_status=fallback:<ErrorType>`。
- resume 已持久化首次上下文，不会重新调用 embedding endpoint；embedding 参数只适用于新的
  `tx run`。

## 受控准入网关

Runtime 写入不能直接信任抽取器提供的 `origin`、`authority`、`scope` 或证据摘要。
`MemoryAdmissionService` 是第一条受控写入路径：抽取器只能提交程序记忆的正文、抽象、cue、
置信度和重要度；网关重新读取经过认证的事务 receipt，并把它与当前持久化 manifest 和
trajectory 逐项核对，然后由 Runtime 固定以下字段：

- 类型为 `procedural`；
- 来源为 `agent`、权限为 `inform`；
- 证据为经过 HMAC 校验的 transaction receipt；
- 作用域为 receipt 中经过认证、在本机稳定的 workspace identity；
- 生效时间为 receipt 的签发时间。

事务已经 abort、receipt 被修改、trajectory 与 receipt 不再一致或验证检查不合格时，准入会在
初始化记忆库之前失败。底层 `SQLiteMemoryStore.add_card()` 仍用于测试、迁移和内部基础设施，
不应成为未来抽取器或模型工具的直接入口。

事务准入网关覆盖经过验证的事务流程和实现补丁；对话桥则是独立的用户来源准入器，只允许
显式命令确认，不能伪装为 transaction receipt。补丁经验固定为 workspace 作用域、
`trusted_tool/inform`，正文来自经过摘要核对的 prepared patch，任务 cue 由中英文词法规则自动
生成；同一 receipt 重放具有原子幂等性。外部资料和普通 Agent 观察仍需要各自的来源解析器，
不能通过伪装成 transaction receipt 或用户确认命令接入。

验证命令正文不会写入 trajectory、receipt 或记忆。Runtime 只把命令 SHA-256 纳入持久化
验证证据，用于证明 receipt 中的检查名称绑定到实际配置；自动生成的卡片只记录检查名称和
“提交前运行已配置验证矩阵”这一流程。记忆写入发生在 commit 之后；写入失败会明确报告
`memory: skipped`，但不会把已经成功的事务误报为失败。

同一 workspace 的验证流程是单一活跃版本：相同命令指纹会幂等追加 receipt 证据；命令指纹
变化会创建新卡并把旧卡标记为 `superseded`。版本顺序使用 receipt 签发时间，延迟到达的旧
receipt 只能形成或补充历史记录，不能覆盖较新的 active 配置。

研究依据和分阶段架构见
[`superpowers/specs/2026-08-17-evidence-bound-temporal-memory-design.md`](superpowers/specs/2026-08-17-evidence-bound-temporal-memory-design.md)。

## 简单效果评测

仓库包含一组完全离线、每次可重复的记忆核心评测：

```bash
.venv/bin/python -m evals.run_memory_evals
.venv/bin/python -m evals.run_memory_evals --json
```

样例覆盖词法与 cue 召回、无关查询不返回结果、时态替代、证据追溯、无证据写入拒绝、
权限洗白拒绝、无 FTS5 回退、业务行篡改和 FTS 索引投毒。报告还会单独展示一个已知限制：
如果查询与记忆没有共享词，也没有预先设置对应 cue，当前无 embedding 的词法检索通常无法
理解纯语义改写。该诊断项不算安全门失败，但用于防止把词法效果误报成语义检索能力。

确定性的 transaction receipt→准入→检索形成评测：

```bash
.venv/bin/python -m evals.run_memory_formation
```

该套件同时检查默认关闭、commit 门、重试幂等、跨 commit 证据归并、作用域隔离和命令指纹
变化；它不调用模型，也不把固定规则结果外推为自由文本自动抽取能力。

## 四路对照评测

跨编码、研究、个人助理和客服的四路消融评测使用同一份语料、问题和评分规则：

```bash
.venv/bin/python -m evals.run_memory_comparison
.venv/bin/python -m evals.run_memory_comparison --json
```

当前固定样例中，新架构的决策准确率为 `100.0%`，传统三层为 `68.8%`，纯 Top-k
召回为 `6.2%`，无记忆为 `43.8%`；新架构相对最强基线提升 `31.2` 个百分点，
错误记忆注入率为 `0%`。这些数字是仓库内受控回归结果，不是公开数据集上的泛化声明。
基线定义、指标、逐例构造、真实模型评测方式和结果解释见
[记忆架构评测说明](memory-evaluation.md)。

使用 `deepseek-flash` 完成的 64 次真实模型请求中，新架构端到端答案准确率为 `100.0%`，
传统三层为 `87.5%`，纯召回为 `81.2%`，无记忆为 `43.8%`；相对最强基线提升
`12.5` 个百分点。该结果是单模型、单次受控实测，不能替代独立留出集和跨模型复测。

另有一套120轮长期纵向评测：持续加入跨作用域同词干扰，并在中途执行替代、过期和遗忘。
最终132张卡片、29个探针下，新架构总体准确率和长期保留率均为`100%`，错误注入率为
`0%`；相对传统三层提升`13.8`个百分点。该评测只覆盖已写入记忆的长期保持，不覆盖模型
自动从对话形成记忆。

## 检索审计

`MemoryContextPack.audit_record()` 可以生成不含查询原文、记忆正文、证据路径和作用域键的
结构化审计。记录包括决策原因、候选数量、分数、召回路由、内容SHA-256和证据数量。
查询指纹默认关闭；只有调用方明确设置 `include_query_fingerprint=True` 时才写入查询SHA-256。

审计记录适合写入受保护的运行轨迹，但它只是检索证据，不允许提升记忆或Agent权限。
