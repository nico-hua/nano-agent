# OpenClaw 与 Hermes Agent 设计启发

> 调研日期：2026-09-15  
> 调研范围：OpenClaw 与 Hermes Agent 的官方 GitHub 仓库、官方文档及其中可确认的源码说明。两者仍在快速迭代，本文记录的是调研当日可以确认的设计，不把版本细节视为稳定 API。

## 1. 调研目的与当前项目基线

本文不是完整功能对比，也不建议把 OpenClaw 或 Hermes Agent 逐项复制到 Nanobot。重点是寻找当前教学项目尚未具备、但能帮助理解 Agent 核心架构的设计，并把生产系统中的复杂实现压缩为可学习、可测试的最小版本。

当前项目基线以 [ARCHITECTURE.md](ARCHITECTURE.md)、[DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md) 和实际源码为准。用户给出的 `nanobot/agent/tools/` 在仓库中不存在，实际工具目录是 [`nanobot/tools/`](../nanobot/tools/)。目前已经具备：

- [`AgentLoop`](../nanobot/agent/loop.py) 与 [`AgentRunner`](../nanobot/agent/runner.py) 的职责分离、按 Session 串行、完整 turn 落盘、停止与 Goal continuation；
- OpenAI-compatible、Anthropic-compatible Provider，单次请求超时、有限 transient retry、统一错误结果；
- Tool 注册、运行时上下文、静态 blocked tools、只读工具批次并行，以及文件、Shell、Web、Cron、Goal、Message、Spawn 和 MCP tools；
- JSONL Session、上下文裁剪、摘要压缩，以及 `MEMORY.md` + `history.jsonl` + cursor 的长期记忆整理；
- workspace Skills 的摘要注入、always 注入、显式激活和依赖检查；
- 持久化的 `at`、`every`、Cron 表达式任务，以及同步/后台 Subagent；
- QQ、WebSocket、HTTP API、流式 `delta` / `tool_call` / `turn_end` 和静态 token 认证。

因此，本文不会把“增加一个工具”“增加一个 Channel”之类的重复能力列为首要启发，而会关注权限决策、执行恢复、记忆检索、循环防护、可靠投递等尚未形成独立边界的能力。

## 2. 两个参考项目体现出的共同方向

OpenClaw 更强调 Gateway 控制面、Channel/节点接入、分层工具策略、审批、沙箱和多 Agent 运行边界。其可复用 Agent Core 将 loop、消息、compaction、skills 和 storage contracts 与 Gateway 分开；Gateway 再负责渠道、认证、调度和宿主能力。参见 [OpenClaw 仓库](https://github.com/openclaw/openclaw)、[Agent runtime architecture](https://docs.openclaw.ai/agent-runtime-architecture) 和 [Tools overview](https://docs.openclaw.ai/tools)。

Hermes Agent 更强调同一 Agent 内核在 CLI、Gateway、API、ACP 和批处理入口上的复用，并提供 session search、工具循环防护、可切换终端后端、后台进程、delegation、Cron、Heartbeat 与插件机制。参见 [Hermes Agent 仓库](https://github.com/NousResearch/hermes-agent)、[Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) 和 [官方文档首页](https://hermes-agent.nousresearch.com/docs/)。

两者共同说明了一个重要原则：Agent 的成熟度不只取决于模型和工具数量，更取决于每次动作的权限边界、运行状态能否恢复、消息是否可靠到达，以及失败后系统能否保持可解释的一致状态。

## 3. 核心 Agent 设计启发

以下设计直接影响模型—工具循环、上下文质量或任务正确性，优先级通常高于增加更多外围集成。

### 3.1 统一的 Tool Policy 与逐次审批

**解决的问题**

当前 `blocked_tool_names` 只能表达“本轮完全不可见/不可执行”，工具自身再分别做路径和参数校验。它不能统一表达“只读操作直接允许、写操作询问一次、危险命令拒绝、无人值守任务默认拒绝”等运行时策略。

**OpenClaw / Hermes 的设计**

OpenClaw 在模型看到工具前，会综合 profile、allow/deny policy、Provider 限制、sandbox 状态、Channel 权限和插件可用性过滤工具；对宿主 `exec`，还叠加 mode、allowlist 和可选用户审批，最终策略取更严格结果。审批绑定具体执行上下文，过期或 turn 被取消后，迟到的批准不能重新启动原操作。参见 [OpenClaw Tools](https://docs.openclaw.ai/tools) 与 [Exec approvals](https://docs.openclaw.ai/tools/exec-approvals)。

Hermes 提供 smart、manual、off 等审批模式，并区分交互式与无人值守入口；Cron/API 等无法等待用户的入口应按预设策略允许或拒绝，而不是永久挂起。它还把文件写入、命令执行、凭据过滤和跨 Session 隔离作为不同防线。参见 [Hermes Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)。

**当前项目状态**

已有 `ToolRegistry`、`RequestContext`、Goal/Subagent blocked tools、workspace 边界和工具级错误，但没有统一策略结果，也没有 pending approval 生命周期。

**最小实现**

1. 增加纯决策组件 `ToolPolicy.evaluate(tool, arguments, request_context)`；结果仅为 `allow`、`deny(reason)`、`require_approval(summary)`。
2. 第一阶段只覆盖 `exec`、`write_file`、`edit_file`、`apply_patch` 和 `message`，其他工具保持现状。
3. `AgentRunner` 在实际执行工具前调用策略；`deny` 生成普通 `ToolResult.error`。
4. 审批记录绑定 `session_key`、`tool_call_id`、经过规范化的参数摘要和过期时间；先只提供 `/approve <id>`、`/deny <id>`。
5. Cron、后台 Subagent 等无人值守来源遇到 `require_approval` 时默认拒绝，避免等待一个不存在的交互者。

**涉及模块**

`nanobot/tools/`、`nanobot/agent/runner.py`、`nanobot/tools/context.py`、`nanobot/agent/commands.py`；若要通过 Channel 展示审批，再涉及 `MessageBus` 和 Web UI。

**收益与复杂度**

收益高：安全规则集中、可测试，后续沙箱和插件都能复用。复杂度中高：必须保证批准与原 tool call 严格关联，取消和超时后不能执行迟到请求。

**建议**

建议作为下一阶段核心功能，但拆成“同步 deny/allow policy”和“可暂停审批”两个小任务；不要一开始复制 OpenClaw 的持久授权、节点审批和复杂 allowlist。

### 3.2 Tool-loop Guardrail：识别无进展的工具循环

**解决的问题**

`max_iterations` 只能在固定次数后停止，无法及早识别模型反复调用同一个失败工具、改变无关参数重复失败，或对幂等读取反复得到同一结果的情况。Goal continuation 还可能把这种无进展循环扩展到多个 Runner turn。

**Hermes 的设计**

Hermes 分别统计“完全相同的失败调用”“同一工具连续失败”“幂等调用返回相同结果且无进展”，先向 tool result 注入警告，达到更高阈值后可硬停止；交互入口默认偏向警告，无人值守 Gateway/Cron 默认启用硬停止。成功的变更型调用会被视为进展并重置相关计数。参见 [Hermes Configuration - Tool-Loop Guardrails](https://hermes-agent.nousresearch.com/docs/user-guide/configuration#tool-loop-guardrails)。

**当前项目状态**

有 `max_iterations`、Provider retry 和 Goal continuation 上限，但没有基于调用签名、错误或结果的“进展”判断。

**最小实现**

在单次 `AgentRunner` 内维护一个小型 `ToolLoopGuard`：

- 对规范化后的 `tool_name + arguments` 和结果摘要计算哈希；
- 相同失败调用连续 2 次时，在下一条 ToolMessage 中附加自我纠正提示；
- 连续 4 次时返回明确的 `stop_reason="tool_loop"`；
- 任何成功的非幂等工具调用都清空计数；
- Goal continuation 将累计 guard 摘要存入 continuation metadata，而不是保存全部结果。

**涉及模块**

主要是 `nanobot/agent/runner.py` 和测试；若跨 continuation 累计，再小范围涉及 `AgentLoop` 或 `GoalState`。

**收益与复杂度**

收益高、复杂度低到中：明显降低浪费和长目标失控概率。难点是第一版不要误判正常的分页读取或轮询，应只覆盖明确相同且失败/结果相同的调用。

**建议**

建议现在实现，优先级高于 Provider fallback。

### 3.3 可管理的后台进程，而不是只有一次性 `exec`

**解决的问题**

当前 `ExecTool` 适合有界、一次性命令。开发服务器、测试 watcher、交互式 CLI 或长时间构建需要“启动后返回句柄、读取增量输出、写 stdin、终止、关闭时清理”，否则 Agent 只能阻塞等待或失去进程所有权。

**OpenClaw / Hermes 的设计**

OpenClaw 的 `exec` 可把命令放到后台，随后由独立 `process` 工具 poll、发送按键或终止；后台 Session 按 agent 隔离，进程退出还可产生完成事件。参见 [OpenClaw Exec](https://docs.openclaw.ai/tools/exec)。Hermes 的架构也把 process registry 和多种 terminal backend 放在工具层之下，使本地、Docker、SSH 等执行共享同一生命周期接口。参见 [Hermes Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) 与 [Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)。

**当前项目状态**

已有 subprocess 超时、取消清理和输出截断，也已在开发计划中列出 `write_stdin`、`list_exec_sessions`，但没有进程注册表。

**最小实现**

1. 增加进程内 `ProcessRegistry`，按 `session_key` 保存 process ID、命令类别、启动时间、状态和有界输出缓冲区。
2. `exec(wait=false)` 只返回不可猜测的句柄；增加 `process` 工具的 `list`、`poll`、`write`、`terminate`。
3. 工具只能访问当前 Session 创建的进程；AgentLoop/Application 关闭时统一取消并等待。
4. 暂不持久化进程，不支持 PTY、SSH 或跨重启恢复。

**涉及模块**

`nanobot/tools/builtin/exec.py`、新进程管理模块、`ToolContext`、`Application` 和对应测试。

**收益与复杂度**

收益高，能显著提升 Coding Agent 的实用性；复杂度中等，关键是不泄漏后台任务、输出内存和跨 Session 权限。

**建议**

建议现在实现。它是当前工具体系中最自然的下一步，也为以后接入沙箱后端提供清晰接口。

### 3.4 跨 Session 的只读检索

**解决的问题**

`MEMORY.md` 适合少量稳定事实，不适合保存全部历史细节。当前 Web UI 能列出和读取 Session，但 Agent 自己无法在用户说“上次那个方案”时检索旧对话，只能依赖已经提取出的长期记忆。

**Hermes 的设计**

Hermes 使用 SQLite FTS5 对历史消息进行全文检索，`session_search` 可按关键词发现 Session，并对结果做有界展开；检索直接返回数据库中的真实消息，不需要先调用 LLM 生成摘要，默认还会减少 tool output 噪声。参见 [Hermes Sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions) 与 [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage)。

**当前项目状态**

Session 使用独立 JSONL 文件，API 有只读 list/get；长期记忆是分类后的压缩事实。开发计划已经列出 `list_sessions`、`search_sessions`、`read_session`。

**最小实现**

- 保留 JSONL 作为权威存储，先增加一个可重建的 SQLite FTS5 side index；
- Session 保存成功后增量更新索引，启动时可显式 rebuild；
- 提供只读 `search_sessions(query, limit)` 与 `read_session(session_id, offset, limit)` 工具；
- 默认只索引 user/assistant 文本，不索引 system prompt、token、凭据或完整 tool output；
- 返回命中片段和 Session ID，由模型按需继续读取，避免一次注入全部历史。

**涉及模块**

`nanobot/session/`、`nanobot/tools/builtin/`、`ToolLoader`，可选地复用 `nanobot/api/service.py` 的序列化逻辑。

**收益与复杂度**

收益高、复杂度中等。它补足“长期事实”和“原始历史”之间的检索层，同时不必立刻引入向量数据库。

**建议**

建议近期实现，先做纯关键词 FTS，不做 embedding、自动召回或 LLM 重排序。

### 3.5 结构化 Prompt 分区与缓存稳定性

**解决的问题**

当前 `ContextBuilder` 每次构造完整 system prompt。即使其中大部分静态内容未变化，Provider 也只看到一个普通字符串，项目无法明确区分稳定前缀、Session 上下文和当前 turn 注入，更难利用不同厂商的 prompt caching 能力。

**Hermes 的设计**

Hermes 的 prompt builder 按稳定、上下文相关和易变内容分层，并强调 system prompt 在会话内保持字节稳定；其架构把 context engine、压缩、prompt caching 和辅助模型调用作为不同边界。参见 [Hermes Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) 与 [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)。

**当前项目状态**

已有 `ContextBuilder`、system/session/current-message 预算、Skill 分区和摘要，但输出仍是扁平消息序列，Provider 没有缓存提示或 capability 声明。

**最小实现**

让 `ContextBuilder` 内部先生成 `PromptSections(stable, session, turn)`，最后仍兼容地输出 `SystemMessage`；为支持 prompt cache 的 Provider 增加可选 adapter，只标记稳定段。第一阶段不改变 Session 格式，也不把 Provider 专有字段泄漏到 AgentLoop。

**涉及模块**

`nanobot/agent/context.py`、Provider message adapter、配置 schema 和 Provider 测试。

**收益与复杂度**

收益中等：降低长会话重复输入成本，并使上下文来源更可审查。复杂度中等，真正收益依赖 Provider 能力，且动态 Skills/Memory 会影响稳定边界。

**建议**

建议在先完成模型 capability 声明后实现；当前不必为了抽象而改动公开消息结构。

### 3.6 Provider capability 声明与受控 fallback

**解决的问题**

当前 ProviderFactory 能创建两类兼容 Provider，重试只处理同一请求的临时错误。系统不知道某模型是否支持 tools、streaming、reasoning 或特定上下文长度，也不能在主模型不可用时按明确策略切换。

**OpenClaw / Hermes 的设计**

OpenClaw 将 auth profile 轮换与模型 fallback 分成两层；fallback candidate 只在符合条件的错误上前进，并且自动 fallback 只影响当前 turn，不篡改用户显式选择。已有可见输出时还要避免把另一模型的完整回答重复发送。参见 [OpenClaw Model failover](https://docs.openclaw.ai/concepts/model-failover)。Hermes 的共享 runtime provider resolver 让 CLI、Gateway、API 等入口使用一致的模型解析、retry/fallback 和能力信息。参见 [Hermes Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)。

**当前项目状态**

已有单次超时、错误分类、有限 retry 和流式“有输出后不重试”不变量，但没有 capability registry、auth profile 轮换或 fallback chain。

**最小实现**

1. 先增加只读 `ProviderCapabilities`：`tools`、`streaming`、`max_context_tokens`，由 factory 校验配置与 AgentRunSpec 是否兼容。
2. 再增加显式 `fallbacks: [provider/model]`，仅对 timeout、429、5xx、连接失败且“尚无可见输出”的首轮请求生效。
3. fallback 只属于当前 turn；用户显式选择模型时默认 strict。
4. 每次尝试保留结构化原因，但用户错误消息不暴露凭据或原始 SDK payload。

**涉及模块**

`nanobot/providers/`、`nanobot/config/schema.py`、`Application` 和 Provider/Runner 测试；AgentRunner 只消费统一 Provider，不自行维护 fallback 循环。

**收益与复杂度**

能力声明收益高且复杂度低；fallback 收益中高但复杂度高，尤其涉及工具调用、流式输出、成本和模型行为差异。

**建议**

建议现在先实现 capability 声明；fallback 放在策略、审批和 loop guard 之后。

### 3.7 把 Heartbeat 与 Cron 区分为两种语义

**解决的问题**

Cron 适合“在某个时间运行一项明确任务”，Heartbeat 适合“当前会话空闲时周期性检查是否有值得报告的变化”。若两者混为一谈，要么丢失当前会话上下文，要么周期任务持续污染主 Session 并产生不必要回复。

**OpenClaw / Hermes 的设计**

OpenClaw 把 Heartbeat 作为 scheduler 持有的 system-owned automation job，在主 Session 中周期运行，并允许用 `HEARTBEAT_OK` 抑制无事发生时的出站消息。参见 [OpenClaw Heartbeat](https://docs.openclaw.ai/heartbeat)。Hermes 明确区分：Heartbeat 在当前 conversation、仅在 turn 之间且空闲时注入；Cron 每次使用 fresh isolated session。忙碌或停机期间错过的多个 Heartbeat 会合并为一次，而不是补跑积压。参见 [Hermes Session Heartbeats](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat) 与 [Scheduled Tasks](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)。

**当前项目状态**

Cron 支持持久化调度并把任务转换为普通 `InboundMessage`，默认复用原 Session 的上下文、历史、压缩和记忆；没有 Heartbeat，也没有“无变化则静默”的协议。

**最小实现**

- 不改变现有 Cron 默认语义，先给 schedule 增加 `context_mode: "session" | "isolated"`，默认 `session` 保持兼容；
- 另做每 Session 最多一个 Heartbeat 配置，只在没有 active turn 时投递；错过的 tick 合并；
- Runner 最终结果为约定的 `HEARTBEAT_OK` 时不发 OutboundMessage，但仍记录一次运行状态；
- Heartbeat 不在运行中注入，避免破坏 tool call/tool result 顺序。

**涉及模块**

`nanobot/cron/`、`Session` 或独立 heartbeat store、`AgentLoop`、CommandRouter 和测试。

**收益与复杂度**

收益中等，能学习“计划调度”和“会话监控”的不同模型；复杂度中等，主要是 idle 判断、去重和 token 成本。

**建议**

建议先实现 Cron 的 isolated mode，Heartbeat 等出现明确监控需求后再做，不要把它包装成无限主动聊天。

### 3.8 Subagent 的可恢复完成通知与资源移交

**解决的问题**

当前后台 Subagent 有状态、并发、超时和取消，但状态只在内存中；完成通知发布失败或进程重启会丢失。子 Agent 启动的后台进程也没有明确方式移交给父 Agent。

**Hermes 的设计**

Hermes 的 child agent 有隔离上下文、终端 Session 和继承后再收窄的工具集，只把最终摘要带回父上下文。后台 delegation 先持久化 completion event，再放入 fresh-turn queue；若处理器缺失、route 不匹配或队列已满，完成事件保留待重试。它还允许在约束下把子 Agent 拥有的后台进程移交给父 Agent。参见 [Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)、[Delegation patterns](https://hermes-agent.nousresearch.com/docs/guides/delegation-patterns) 和 [官方源码说明](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/delegation.md)。OpenClaw 同样默认让 Subagent 使用独立 Session，并把结果回告请求者。参见 [OpenClaw Sub-agents](https://docs.openclaw.ai/tools/subagents)。

**当前项目状态**

已有独立 ToolRegistry、禁止递归 Spawn、后台状态机、原路由回传、单次发布和关闭取消；缺少持久化任务/完成事件、进程资源移交和重启恢复。

**最小实现**

在实现 `ProcessRegistry` 后，再增加一个 JSONL `SubagentCompletionStore`：

- 后台任务结束先原子写入 completion record，再尝试 `MessageBus.publish_inbound`；
- record 保存 `pending_delivery/delivered`，启动时重投 pending，但使用稳定 completion ID 去重；
- 只持久化最终摘要和原始路由，不持久化完整 child transcript；
- 第一版不恢复“正在运行”的 Python coroutine，只把重启时的 running 标为 interrupted。

**涉及模块**

`nanobot/subagent/`、`MessageBus`、`Application` 生命周期；进程移交还涉及未来的 `ProcessRegistry`。

**收益与复杂度**

收益中高：避免长任务做完却无法通知。复杂度中高，核心是 at-most-once 状态转换与消息投递的幂等边界。

**建议**

建议在可靠投递最小模型确定后实现；当前先不要尝试跨重启恢复正在执行的 AgentRunner。

### 3.9 Skill 的信任、检疫和受控演化

**解决的问题**

Skill 是会进入 prompt 的指令，即使项目“不执行 SKILL.md 中的命令”，恶意或被篡改的 Skill 仍可能诱导模型调用高权限工具、泄露信息。未来若允许 Agent 自己创建 Skill，还会出现错误流程被永久固化的问题。

**OpenClaw / Hermes 的设计**

OpenClaw 把 tools、skills、plugins 分开：Skill 是按需加载的说明，Plugin 才注册运行时代码；第三方 Skill 被视为不可信内容，路径必须保持在可信根内，安装还可经过外部 policy。参见 [OpenClaw Skills](https://docs.openclaw.ai/tools/skills)。Hermes 对 project skills 做内容哈希扫描，危险内容进入 quarantine，不出现在索引且无法按名加载；Agent 管理的 Skill 被定位为 procedural memory，并可在写入前设置人工审核。参见 [Hermes Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)。

**当前项目状态**

已有路径安全、静态读取、依赖可用性、always/explicit 注入；没有来源标记、内容检疫、安装流程或 Agent 自主维护。

**最小实现**

1. `SkillInfo` 增加 `source`、内容哈希和 `enabled/disabled_reason`。
2. 对 prompt injection 高风险模式只做明确、保守的静态检查，并把结果标为“需人工复核”，不要声称能自动判定安全。
3. Skill 文件变更后重新检查；不可用或待复核 Skill 不注入 prompt。
4. 若以后增加 `skill_manage`，先写入 staging 目录，用户批准后再移动到 `workspace/skills`。

**涉及模块**

`nanobot/skills/loader.py`、`ContextBuilder`、可选 CommandRouter；不应让 `AgentRunner` 解析 Skill。

**收益与复杂度**

收益中等，复杂度中等。静态扫描不是安全边界，真正防护仍依赖 Tool Policy 和 sandbox。

**建议**

现在可增加来源与哈希；自动创建/更新 Skill 应等审批机制完成后再做。

### 3.10 把 Browser 视为有状态执行环境

**解决的问题**

`web_search` 和 `web_fetch` 适合搜索与读取静态公开文本，但无法处理依赖 JavaScript 的页面、登录态、点击/表单交互和多步骤网页工作流。简单地给 `web_fetch` 增加更多 HTML 解析不能解决浏览器状态与权限问题。

**OpenClaw / Hermes 的设计**

OpenClaw 使用独立的 agent-only 浏览器 profile，由 Gateway 内的本地控制服务管理，默认不接触用户日常浏览器；另有明确选择才连接真实已登录浏览器。浏览器工具暴露导航、页面读取、点击和输入等动作。参见 [OpenClaw Browser](https://docs.openclaw.ai/tools/browser)。Hermes 同样把 `browser_navigate`、`browser_snapshot`、`browser_vision` 等归入独立 Browser toolset，而不是塞进普通 HTTP 工具。参见 [Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)。

**当前项目状态**

有受限 `web_fetch`、Tavily `web_search` 和本地 Shell，没有浏览器进程、页面句柄、登录态隔离或浏览器动作审批。

**最小实现**

先增加单一、本地、无个人登录态的 `BrowserSessionManager`，按 `session_key` 隔离页面；第一版只提供 `navigate`、可访问性/文本 `snapshot`、`click`、`type` 和 `close`。导航复用 `web_fetch` 的公网地址限制，下载、文件选择、任意 JavaScript evaluate 和连接个人浏览器默认禁用。所有变更型动作经过 Tool Policy。

**涉及模块**

新 `nanobot/browser/`、browser builtin tools、`ToolContext`、`Application` 生命周期和可选依赖配置；AgentRunner 与 Channel 不感知浏览器 SDK。

**收益与复杂度**

收益中高，能覆盖真实网页交互并清楚展示“有状态工具服务”的设计；复杂度高，包含进程清理、页面状态、SSRF、下载和凭据边界。

**建议**

建议暂缓到 Tool Policy 与 ProcessRegistry 完成后，再做无登录态的最小 Playwright 实现；不要直接复用开发者个人浏览器 profile。

## 4. 生产级增强设计

以下能力很有价值，但主要解决多用户、长时间运行、故障恢复或高风险执行问题。它们不应抢在核心循环可解释性之前实现。

### 4.1 可替换的执行后端与 OS sandbox

**解决的问题**

workspace realpath 校验只能保护项目内置文件工具，不能约束任意 Shell 命令的文件、网络、系统调用和凭据访问。

**参考设计**

OpenClaw 的 Gateway 留在宿主机，仅把工具执行移入 sandbox，并将 mode（何时隔离）、scope（按 agent/session/shared）和 backend（Docker、Podman、SSH 等）分开；workspace 可选择不挂载、只读或读写。参见 [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing)、[Modes, scope, and backend](https://docs.openclaw.ai/gateway/sandboxing/modes-scope-and-backend) 与 [Workspace access](https://docs.openclaw.ai/gateway/sandboxing/workspace-access)。Hermes 也使用统一 terminal backend 支持 local、Docker、SSH 和多种远程 sandbox。参见 [Hermes Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)。

**当前项目与最小实现**

当前 `ExecTool` 是本地 subprocess，不是 sandbox。最小版本应先定义 `ExecutionBackend.run()`，保留 Local 实现，再增加可选 Docker backend；只把 `exec` 接入，不迁移全部文件工具，不做 SSH/云后端。

**模块、收益、复杂度与建议**

涉及 `ExecTool`、ToolContext、配置和 Application 生命周期。安全收益高，环境与跨平台测试复杂度高。建议在 ProcessRegistry 和 Tool Policy 之后实现，默认仍可关闭，但高风险/外部用户入口应 fail closed。

### 4.2 执行结果与投递结果分开建模

**解决的问题**

AgentRunner 成功不代表 QQ/WebSocket 最终收到消息。当前 MessageBus 是内存队列，ChannelManager 记录发送异常后继续，但没有 receipt、重投或可查询状态。

**参考设计**

Hermes Cron 将 agent execution 与 delivery 分开记录：运行成功但目标平台未给出成功证据时使用 `delivery_failed`，而不是把任务记为成功；Bot Chat 等异步目标还使用持久 completion/delivery receipt，拒绝把“已入队”误认为“已完成”。参见 [Hermes Scheduled Tasks - Delivery failures](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron#delivery-failures-are-a-distinct-status)。

**当前项目与最小实现**

先给主动消息、Cron 和 Subagent 结果增加稳定 `delivery_id`，Channel `send` 返回 `DeliveryResult(status, external_id, error)`；以 JSONL 保存 `pending/sent/failed/ambiguous`。不要立刻为普通流式 delta 做持久重投，因为没有 `turn_id` 时无法可靠去重。

**模块、收益、复杂度与建议**

涉及 `OutboundMessage`、MessageBus、ChannelManager、Cron/Subagent store。收益高但属于生产可靠性，复杂度高。建议从 Cron 最终结果开始，而不是一次改造所有消息。

### 4.3 生命周期事件与 Hook，但只为明确消费者服务

**解决的问题**

记忆、压缩、审计、指标和未来插件若都直接插入 AgentLoop，会逐渐让核心流程难以阅读；但过早建立万能事件总线也会隐藏控制流。

**参考设计**

OpenClaw 区分短小的内部 hooks、可修改行为的 typed plugin hooks、HTTP webhooks 和仅用于遥测的 diagnostic events。内部 handler 按顺序 await，单个异常不阻断后续 handler；长任务必须交给有生命周期的 automation/service，而不能从 hook 中偷偷创建无管理任务。参见 [OpenClaw Hooks](https://docs.openclaw.ai/automation/hooks) 与 [Plugin hooks](https://docs.openclaw.ai/plugins/hooks)。

**当前项目与最小实现**

当前多个副作用由 AgentLoop 直接调用。最小版本只增加进程内、强类型、只观察的 `turn_completed`、`turn_failed`、`tool_started`、`tool_finished` 事件，首个消费者必须是审计或指标；handler 顺序 await、有超时、不能修改结果。

**模块、收益、复杂度与建议**

涉及 `AgentLoop`、`AgentRunner` 或独立 events 模块。收益中等，复杂度中等。没有真实消费者时暂不实现，以免为了抽象而抽象。

### 4.4 Coding checkpoint 与保守回滚

**解决的问题**

`apply_patch` 能保证单次 patch 原子性，但一个长 Coding 任务可能跨多次工具调用修改多个文件。用户目前无法把“本轮 Agent 改动”整体回到某个安全点。

**Hermes 的设计**

Hermes 在文件写入或破坏性命令前创建 checkpoint，使用独立 shadow Git store，不修改项目真实 `.git`；恢复时还检查 Agent 最后写入后的文件哈希，用户后来改过的文件不会被盲目覆盖。该能力默认关闭，因为存储和生命周期并不便宜。参见 [Checkpoints and /rollback](https://hermes-agent.nousresearch.com/docs/user-guide/checkpoints-and-rollback)。

**当前项目与最小实现**

先实现可选 `WorkspaceCheckpoint`，仅在 `write_file`、`edit_file`、`apply_patch` 前保存受影响 UTF-8 文件内容与哈希；`/rollback` 只恢复哈希仍与 Agent 最后写入一致的文件。不要第一版拦截任意 Shell，也不要操作用户 Git 历史。

**模块、收益、复杂度与建议**

涉及文件工具、RequestContext、独立 checkpoint store 和命令。对 Coding Agent 收益高，复杂度中高。建议在 Tool Policy 后作为可选能力实现。

### 4.5 Profile 级隔离，而不是把“多 Agent”理解为互相聊天

**解决的问题**

单 workspace、单配置适合当前教学项目；若未来同时运行“代码 Agent”和“个人助理”，共享 Session、Memory、Skills、凭据和 Cron 会产生身份与权限串扰。

**Hermes 的设计**

Hermes profile 是独立 home：配置、API key、memory、sessions、skills、cron 和 gateway state 全部分开；Bot Mode 只是 profile 的 UI，而不是另一套 Agent 内核。官方还明确警告两个进程不要同时写同一 profile。参见 [Hermes Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) 与 [Bot Mode](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode)。

**当前项目与最小实现**

当前 `Application` 组合一个 workspace。最小版本可让 CLI 通过 `--profile` 选择独立配置根和 workspace，再创建一套 Application；不要先实现 Agent-to-Agent 消息、共享记忆或动态图编排。

**模块、收益、复杂度与建议**

涉及 config paths、CLI 和 Application 组合根。收益取决于实际多身份需求，复杂度中等。建议暂缓；当前 Subagent 已足够用于同一主 Agent 下的任务分解。

### 4.6 外部事件触发复用现有任务，而不是增加另一套 Agent 入口

**解决的问题**

固定时间轮询会浪费请求并增加延迟。CI、代码评审、告警系统更适合在事件发生时触发已有任务，但事件入口若直接调用 Provider，会绕过 Session、工具权限、错误处理和投递状态。

**Hermes 的设计**

Hermes Webhook route 可在通过 HMAC、过滤和幂等检查后触发现有 Cron job；事件 payload 只作为单次运行的临时上下文，不修改任务保存的 prompt，并复用调度器的 at-most-once claim 与原投递目标。参见 [Hermes Webhooks - Event-Triggered Cron Jobs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks#event-triggered-cron-jobs)。OpenClaw 也把 HTTP webhooks、内部 hooks 和 scheduler automation 明确区分，避免用进程内 hook 承载耐久任务。参见 [OpenClaw Hooks](https://docs.openclaw.ai/automation/hooks)。

**当前项目与最小实现**

当前 HTTP API 能发普通消息，Cron 只按时间触发。最小版本是在 API 增加带静态 token 的 `POST /v1/cron/{job_id}/trigger`：校验任务存在且未在运行，把请求体中有界的 `event` 文本作为本次 metadata/context，经 CronService 的统一 claim 和 callback 进入 `InboundMessage`；不直接调用 AgentLoop 或 Provider，也不永久改写 Cron payload。

**模块、收益、复杂度与建议**

涉及 `nanobot/api/`、`nanobot/cron/` 和认证测试。收益中等、复杂度中等。建议等 Cron 有明确的 in-flight 去重和 execution record 后实现，第一版不做任意模板表达式。

## 5. 已有相似能力，不建议重复实现

以下方向在参考项目中也很重要，但当前项目已经抓住了主要设计思想，短期应优化现有实现而不是另建一套：

- **AgentLoop / AgentRunner 分层**：当前已经把 Session、命令、路由与模型—工具循环分开，与两份参考架构的核心边界一致。
- **Skills 渐进披露**：当前 always Skill 注入正文、普通 Skill 只注入摘要、显式 `$skill-name` 才加载正文，已经覆盖按需加载的关键思想。
- **Subagent 隔离**：当前子 Agent 不继承主历史、使用独立 ToolRegistry 并禁止递归 Spawn；缺的是持久完成通知，而不是再造一种 Spawn。
- **Cron 经正常消息链进入 Agent**：当前 Cron 不直接调用 Channel 或 Provider，保留 Session、工具与错误边界。是否使用原 Session 或 isolated context 应作为策略，而不是重写调度器。
- **流式事件协议**：当前 `delta`、`tool_call`、`turn_end` 已有顺序约束。没有 `turn_id` 时，不宜贸然增加流式断点续传或重投。
- **Provider transient retry**：当前已经有 timeout、连接错误、429/5xx 的有限重试和“有 delta 后不重放”。下一步是 capability/fallback，不是再叠一层 AgentRunner retry。

## 6. 推荐实施顺序

### P0：下一阶段最值得学习和实现

1. **Tool-loop Guardrail**：改动小，直接降低普通 turn 与 Goal 的无进展循环风险。
2. **Tool Policy 的 allow/deny 纯决策层**：先统一权限判断，不立刻实现复杂异步审批。
3. **ProcessRegistry + `process` 工具**：补齐 Coding Agent 对长进程的基本生命周期管理。
4. **Session FTS5 side index + 只读检索工具**：建立长期事实与完整历史之间的检索层。

### P1：核心边界稳定后实现

5. **逐次工具审批**：基于 Tool Policy，先覆盖高风险工具，并正确处理取消、超时和无人值守入口。
6. **Provider capability 声明**：在配置阶段拒绝不兼容的 tools/streaming 请求。
7. **Subagent completion 持久化**：先保证“完成结果不会因瞬时队列失败丢失”，不恢复运行中的 coroutine。
8. **Cron isolated context mode**：让自包含定时任务可选择不污染对话 Session。
9. **可选 Coding checkpoint**：为跨多次文件修改提供保守回滚。

### P2：出现明确生产需求后实现

10. **有限模型 fallback**：仅在无可见输出且错误明确可切换时使用，保持用户显式模型选择严格。
11. **Docker ExecutionBackend**：在本地 backend 接口和 Tool Policy 稳定后增加。
12. **Heartbeat**：复用 scheduler，但保持 idle-only、错过 tick 合并和静默结果语义。
13. **Delivery receipt**：先覆盖 Cron/Subagent/主动消息，暂不重投流式 delta。
14. **无登录态 BrowserSessionManager**：先提供最小动作集，并复用地址限制、审批和进程清理。
15. **外部事件触发 Cron**：在任务 claim 与 execution record 稳定后复用现有调度路径。
16. **Profile 隔离、typed hooks、Skill staging/quarantine**：分别在多身份、真实扩展消费者或 Skill 安装需求出现时推进。

## 7. 暂时不建议引入的复杂度

- 不复制 OpenClaw 的完整 Gateway 节点协议、宿主级 durable grants、角色与多租户控制面；当前只有本地单管理员边界。
- 不复制 Hermes 的全部远程 terminal backend、云 sandbox 和 Bot 群体协作；先把本地进程生命周期做正确。
- 不引入向量数据库或自动把跨 Session 检索内容塞入每一轮 prompt；FTS5 按需读取更符合当前项目规模。
- 不允许 Agent 在没有审批和检疫的情况下自动安装插件、修改 Skill 或扩大自己的工具权限。
- 不在没有幂等 ID 和 delivery receipt 前实现“看起来可靠”的自动消息重投。
- 不把 Hook 变成第二条隐藏业务主流程；核心状态转换仍应在明确的 service/manager 中完成。

## 8. 官方资料索引

### OpenClaw

- [GitHub repository](https://github.com/openclaw/openclaw)
- [Agent runtime architecture](https://docs.openclaw.ai/agent-runtime-architecture)
- [Tools overview](https://docs.openclaw.ai/tools)
- [Exec tool](https://docs.openclaw.ai/tools/exec)
- [Exec approvals](https://docs.openclaw.ai/tools/exec-approvals)
- [Skills](https://docs.openclaw.ai/tools/skills)
- [Sub-agents](https://docs.openclaw.ai/tools/subagents)
- [Memory overview](https://docs.openclaw.ai/concepts/memory)
- [Model failover](https://docs.openclaw.ai/concepts/model-failover)
- [Heartbeat](https://docs.openclaw.ai/heartbeat)
- [Hooks](https://docs.openclaw.ai/automation/hooks)
- [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing)
- [Browser](https://docs.openclaw.ai/tools/browser)

### Hermes Agent

- [GitHub repository](https://github.com/NousResearch/hermes-agent)
- [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- [Sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions)
- [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage)
- [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Scheduled Tasks](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Session Heartbeats](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat)
- [Checkpoints and rollback](https://hermes-agent.nousresearch.com/docs/user-guide/checkpoints-and-rollback)
- [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
