# Harbor 固定模型 Harness 对照：三任务 smoke

日期：2026-08-26

状态：三任务 smoke 已完成；25 任务 pilot 暂不放行。本文是工程可靠性诊断，不是公开
leaderboard 或模型能力声明。

## 结论

任务按锁定 `pilot-25.json` 的既有顺序取前 3 个，没有按难度或结果挑题：

1. `matplotlib__matplotlib-14623`
2. `django__django-16938`
3. `sympy__sympy-16886`

| 任务 | baseline 原始 reward | candidate 原始 reward | candidate Agent | 工程证据判定 |
| --- | ---: | ---: | --- | --- |
| Matplotlib 14623 | 1 | 0 | 正常提交 | 精确 `prepared.patch` 重放为 1；原始 0 保留为 verifier 基础设施错误 |
| Django 16938 | 1 | 0 | 三次恢复耗尽，事务 open | unresolved；部分补丁 verifier 失败，禁止重放成成功 |
| SymPy 16886 | 1 | 1 | 正常提交 | resolved |

聚合结果：

- baseline Harbor 原始 resolved：`3/3 = 100%`；Agent exception：`0/3`。
- candidate Harbor 原始 resolved：`1/3 = 33.3%`。
- candidate 带独立重放证据的工程 resolved：`2/3 = 66.7%`，但不得覆盖原始分数。
- candidate Agent transport exception：`1/3`（Django）。
- candidate verifier infrastructure exception：`1/3`（Matplotlib）。
- candidate 真实未解决：`1/3`（Django）。

这组结果证明事务恢复可以在连接中断后保留并继续同一轨迹，但 3 次恢复仍可能耗尽。因为
small smoke 已出现 `1/3` Agent transport exception，直接扩大到 25 个付费任务会放大噪声，
当前协议状态设为 `three-task-smoke-complete-pilot-blocked`。

## 固定协议与任务锁

- Harbor：`0.22.0`
- 数据集内容 SHA-256：
  `b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`
- 模型：`openai/gpt-5.6-sol`
- Endpoint：`https://api.dstopology.com/v1`
- baseline：`mini-swe-agent==2.1.0`
- candidate：`mini-code-agent-langgraph==0.5.0`
- attempts：每任务 1 次；Harbor retry 与模型请求 retry 均为 0
- candidate transaction resume：最多 3 次，退避 10/20/30 秒
- 并发：1
- Docker：`linux/amd64`
- 两臂共享 Agent 环境：`BASH_ENV=/root/.local/bin/env`、
  `UV_CONCURRENT_DOWNLOADS=1`、`UV_HTTP_TIMEOUT=120`
- Verifier：`UV_CONCURRENT_DOWNLOADS=1`、`UV_HTTP_TIMEOUT=120`

新增任务的两臂 lock 完全匹配：

| 任务 | Harbor task digest | SWE-bench image digest |
| --- | --- | --- |
| Django 16938 | `sha256:9cbf9b0948d6670ebe086e76d603221fc1ce6ea4c6da20c00a136890524238a4` | `sha256:f4346d8fc89a1359ed5ee8429d408ed645f7f8db97ba347ab8d77bfd59e67a4f` |
| SymPy 16886 | `sha256:15b612738dbf2fa5b04bca63af1fcec35a1cb82d214d80dfe471885e053e8abe` | `sha256:8e68941a90d2b34aaedf409876a2353e3ffc7cb8b70c90bb543bea626c5da085` |

Matplotlib 的原始 task/image digest 仍以单任务报告为准。

## 资源

| 任务/Agent | Model calls | Input tokens | Cached input | Output tokens | Reasoning tokens | 上游成本 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Matplotlib baseline | 17 | 276,958 | 31,744 | 4,209 | 1,111 | `$1.0777336` |
| Django baseline | 21 | 418,031 | 36,352 | 5,048 | 未单列 | `$1.6422168` |
| SymPy baseline | 10 | 77,081 | 0 | 2,817 | 未单列 | `$0.3646640` |
| Matplotlib candidate | 26 | 375,787 | 15,872 | 2,696 | 895 | 未报告 |
| Django candidate | 16 | 174,751 | 25,600 | 2,554 | 1,656 | 未报告 |
| SymPy candidate | 8 | 51,030 | 33,280 | 662 | 151 | 未报告 |
| **baseline 合计** | **48** | **772,070** | **68,096** | **12,074** | — | **`$3.0846144`** |
| **candidate 合计** | **50** | **601,568** | **74,752** | **5,912** | **2,702** | — |

新增两任务的 Harbor job 总耗时：baseline `31m57s`，candidate `23m45s`。连同
Matplotlib 单任务 smoke，累计 job 时间约为 baseline `54m33s`、candidate `36m04s`。
这些是 Apple Silicon 上的 amd64 模拟诊断时间，不能外推为原生 x86 吞吐量。

## Django candidate 失败边界

Django candidate 使用同一 transaction：

- 初次连接失败后工作区零改动；事务安全保持 open。
- 第 1 次恢复再次连接失败。
- 第 2 次恢复进入正常 Agent loop，并持续累计到 16 steps/16 model calls。
- 期间生成 `django/core/serializers/python.py` 与
  `django/core/serializers/xml_serializer.py` 的部分修改。
- 后续连接再次失败，第 3 次恢复额度耗尽。
- 最终 `exit_status=Error:OpenAIConnectionError`、`verification_status=required`、
  `transaction_status=open`；没有 commit receipt。
- Harbor 记录 `NonZeroAgentExitCodeError`。隐藏 verifier 对未完成补丁给出
  `resolved=false`，FAIL_TO_PASS 目标未通过，因此没有补丁重放理由。

这不是 verifier false zero，也不是可覆盖的 infrastructure-only reward。它是 candidate
在固定恢复预算内未完成任务的真实可靠性失败。

## SymPy candidate 成功边界

SymPy candidate 在 8 steps/8 model calls 内将错误的 Morse `1` 映射从 `"----"` 修正为
`".----"`，`git diff --check` 通过后事务提交。Harbor 隐藏 verifier 结果：

- `FAIL_TO_PASS`: `test_encode_morse` 通过；
- 42 个计分 `PASS_TO_PASS` 测试全部通过；
- `resolved=true`、原始 reward `1`。

`prepared.patch` SHA-256：

`a50328b99218b5ba1700715b18873104fd3521660e654c38c0d041a458b15849`

Verifier report SHA-256：

`54d6f49054774b7bd92b3ec99c2fc58116fa8376214cf3be839c5c2c78d24096`

## 预检暴露的 Harness 卡点

正式付费 run 前执行了 task/image 下载、amd64 容器启动和 `--install-only`：

1. SymPy 任务 Dockerfile 三次因 GitHub TLS 提前关闭而无法下载固定 `uv 0.7.13`。
   原始 Dockerfile 未修改；有限构建预热成功后 Harbor 复用同一 BuildKit layer。
2. 第一轮正式目录在 Django baseline Agent setup 阶段再次遇到 TLS 错误，且没有模型
   调用。该轮立即中止并作为无效基础设施记录保留，不计入上表。
3. 根因是上游 mini-swe-agent 安装器在加载 `$HOME/.local/bin/env` 前检查 `uv`，会重复
   进入未固定的网络安装器。
4. 两臂统一注入 `BASH_ENV=/root/.local/bin/env` 后，Django 与 SymPy 的 baseline
   `--install-only` 均通过，并确认实际复用任务镜像的 `uv 0.7.13`。

无效预检/中止记录与正式结果分目录保存；没有删除或改写失败证据。

## 证据哈希

- 新增两任务 baseline job result：
  `437319c977b9a2a4bfe83614952021ad9a9fa10fc410a3ec98cdd2b4e4432ba0`
- 新增两任务 candidate job result：
  `f0c59eb1af03a47b76116fc84a42d221dbeda223401af2ee469c0a789d1a6f8f`
- Django candidate trial result：
  `a938a5ead1bee99cb6902df0c55896f12be70fa112353aab654d8c10a267e41f`
- Django candidate transaction trajectory：
  `262b35659ba919dce8bc386b41f360cc7c6f062e49dcf3d136fa75c63bc1e7b6`
- SymPy candidate trial result：
  `f85e5e75a58f9992b6090a0f0b4710519109ca5d4df67fcacbce1b0f2fd05120`

原始目录位于
`artifacts/harbor/fixed-model-smoke-3task-20260826/paid-two-path-ready/`。目录包含完整
任务 prompt、trajectory 和 workspace，不进入 Git，也不能未经脱敏直接发布。

## 25 任务 pilot 的解除条件

1. 在原生 x86-64 主机上复跑三任务安装预检，避免把 QEMU 网络/时间开销混入结论。
2. 使用相同 Endpoint 连续完成三任务且 candidate Agent transport exception 为 0。
3. 保持请求 retry 为 0；如果继续使用 transaction resume，预算和退避不得根据题目结果调参。
4. 保留原始 Harbor reward、Agent exception、Verifier exception 与任何独立重放结果。
5. 满足以上条件后才启动 25-task pilot；暂不扩展 SFT、DPO 或 RL。
