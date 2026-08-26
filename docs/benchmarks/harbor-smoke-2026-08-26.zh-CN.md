# Harbor 固定模型 Harness 对照：实测记录

日期：2026-08-26

状态：单任务 smoke 已完成；25 任务 pilot 尚未开始。本文是工程诊断记录，不能当作公开
leaderboard 结果。

## 结论

在同一个 SWE-bench Verified 任务、同一个 `gpt-5.6-sol` 模型和同一个 Docker
任务镜像上，两套 harness 最终都生成了可通过隐藏验证的补丁：

| 项目 | mini-swe-agent baseline | MCA 0.5.0 candidate |
| --- | ---: | ---: |
| 任务 | `matplotlib__matplotlib-14623` | 同左 |
| agent 异常 | 0 | 0 |
| Harbor 原始 reward | 1 | 0 |
| 精确补丁 verifier 重放 | 不需要 | 1 |
| 最终工程判定 | resolved | resolved；原始 0 为 verifier 基础设施错误 |

candidate 的 Harbor 原始记录不能直接改写。它保留 `reward=0`，因为隐藏测试完成后，
SWE-bench 结果解析器从 PyPI 下载 `aiohttp` 时连接中断，没有输出最终判定标记。使用同一
`prepared.patch`、同一任务镜像和同一 `/tests/test.sh` 做零模型调用重放后，解析器输出
`PASSED`：`FAIL_TO_PASS` 目标测试通过，所有纳入评分的 `PASS_TO_PASS` 测试也通过。

因此，本次 smoke 证明 adapter 和事务边界可以工作，但也证明不能只看 Harbor 顶层的
reward：必须把 agent failure、verifier failure 和真实未解题分开。

## 固定协议

- Harbor：`0.22.0`
- 数据集：`swe-bench/swe-bench-verified`
- 数据集内容 SHA-256：
  `b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`
- 任务 digest：
  `sha256:97fcd85294cffa89e2a411a4860c2e540c76402b82812d2771c04c4967fafb0d`
- SWE-bench 镜像 digest：
  `sha256:5f6acd976c3bc2b2f293704f88b1188307d116baa87b87e5fa82a67976f157a9`
- 模型：`openai/gpt-5.6-sol`
- Endpoint：`https://api.dstopology.com/v1`
- baseline：`mini-swe-agent==2.1.0`
- candidate 包：`mini-code-agent-langgraph==0.5.0`
- candidate：memory off、shell on、Harbor Docker 外层沙箱、MCA 内层 sandbox none
- 尝试：每任务一次；Harbor retry 关闭
- candidate 请求 retry：关闭
- candidate transaction resume：最多 3 次，10/20/30 秒退避
- Docker 平台：`linux/amd64`

## 资源与轨迹

| 指标 | baseline | candidate |
| --- | ---: | ---: |
| Agent steps | 17 | 26 |
| Model calls | 17 | 26 |
| Input tokens | 276,958 | 375,787 |
| Cached input tokens | 31,744 | 15,872 |
| Output tokens | 4,209 | 2,696 |
| Reasoning tokens | 1,111 | 895 |
| Agent 轨迹耗时 | 未单列 | 589.586 秒 |
| Harbor job 总耗时 | 22 分 36 秒 | 12 分 19 秒 |
| 上游报告成本 | 1.0777336 美元 | 当前 MCA provider 未报告成本 |

candidate 最终事务状态为 `committed`，修改了
`lib/matplotlib/axes/_base.py` 和 `lib/matplotlib/tests/test_axes.py`，agent 可见的
`git diff --check` 已通过。candidate `prepared.patch` SHA-256 为：

`508ad448783ffb43df77d8d7d6b36aa5ab51b47a8dfa5b1c9194ee9fa0629fbd`

verifier 重放日志 SHA-256 为：

`5cc7325b844bdf6a209058610fcce9ed6d171fe20a1c026eb7c0d02952a4ccc2`

重放报告 SHA-256 为：

`bb491e21de1472802b92c00741e0ae24e946e782a7bff058019532250be98d29`

## 实际暴露的卡点

1. v0.5.0 launcher 把 `swe-bench/` 从任务名中去掉，Harbor 0.22 因此筛不到任务。
2. 直接执行虚拟环境 Python 时，子进程可能找不到同环境的 `harbor` 可执行文件。
3. Harbor console script 不会自动把仓库根目录放入 import path，candidate adapter 无法导入。
4. Apple Silicon 必须显式使用 `linux/amd64`；否则 SWE-bench 镜像无法解析。
5. agent 安装先检查 PATH、后加载 `$HOME/.local/bin/env`，导致重复下载已有的 `uv`，
   还会把固定的 `uv 0.7.13` 漂移到最新版。
6. 安装完成后的正式 agent shell 没恢复工具 PATH，曾出现 `mca: command not found`。
7. GitHub、PyPI 和中转模型连接都出现过 TLS 提前关闭；高并发下载尤其不稳定。
8. MCA 在模型连接失败后正确保留了 open transaction 和安全 checkpoint，但旧 adapter
   不会执行输出中的 `mca tx resume`。
9. adapter 在 run 阶段提前写入 context metadata，使 Harbor 认为 context 非空，跳过
   `populate_context_post_run`，顶层 result 因而漏掉 MCA token 统计。
10. 隐藏测试已经完成时，结果解析器仍可能因临时依赖下载失败把 resolved 补丁记为 0。

## 已完成的收口

- 任务引用统一成 Harbor 要求的完整 `swe-bench/...` 形式，同时兼容 CLI 的裸任务名。
- 自动定位 active Python 环境旁的 Harbor，并给子进程注入仓库根目录 `PYTHONPATH`。
- 协议锁定 `linux/amd64`、agent/verifier 的低并发下载和读超时。
- candidate 优先复用镜像中的 `uv 0.7.13`，固定包安装最多做三次纯传输重试。
- 每个正式 agent shell 都恢复 MCA 工具 PATH。
- open transaction 最多 resume 三次并退避；每次恢复继续累计 steps/tokens，并使旧验证失效。
- 延迟写入 Harbor context metadata，使下载轨迹后可以填充 usage 和事务结果。
- 保留原始 Harbor 结果，不覆盖失败证据；另存带 hash 的 verifier 重放证据。

## 25 任务 pilot 前的门槛

1. 合并本次 launcher/adapter hardening，并从干净 checkout 再做一次 launcher dry-run。
2. 先选 3 个不同仓库任务做小批量 smoke，确认 verifier 低并发设置不再产生 false zero。
3. 原始结果与任何重放结果必须分目录保存；报告中禁止静默替换 reward。
4. pilot 使用同一模型、Endpoint、协议、attempts 和 concurrency；两臂任务 lock 必须逐项一致。
5. 单独统计 resolved、agent exception、verifier exception 和基础设施重放，不把它们压成一个均值。
6. Apple Silicon 结果只用于工程诊断；正式可发布 pilot 优先在原生 x86-64 主机执行。
