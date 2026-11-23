# 会话压缩机制

一次任务可能在多次模型调用之间积累很长的工具结果。Nano Agent 在**每次业务模型调用前**检查上下文预算。只有请求放不下时，才把较早的连续消息合并为一条摘要，保留近期原文供模型继续工作。完整消息仍保存在 Session 中，因此摘要里的重要结论可以按索引回查。

## 一次请求如何组装

每轮开始，`AgentLoop` 读取 Session，`ContextBuilder` 按下面的顺序组装消息。如果还没有摘要，直接省略第二项。

1. 系统提示词，包括工作区指令、技能和长期记忆。
2. 已有会话摘要，作为独立的 `HumanMessage`。
3. `session.messages[summary_until:]` 中尚未被摘要覆盖的原文。
4. 本轮用户消息原文。

`summary_until` 是摘要覆盖范围的**右开边界**。例如值为 8，表示索引 0～7 已进入摘要，下一条保留的原文是索引 8。轮内压缩后仍采用同样的顺序：系统提示词、更新后的摘要、保留下来的近期原文。随后产生的工具调用、结果和用户补充依次追加。[上下文组装](../nanobot/agent/context.py) · [模型调用循环](../nanobot/agent/runner.py)

## 什么时候压缩，保留哪些消息

| 入口 | 触发条件 | 保留近期原文的方式 |
| --- | --- | --- |
| 主 Agent 或子任务的 Runner | 普通或流式调用前，估算的消息大小超过本次消息预算 | 从最新消息向前选取能放下的连续原文。如果边界切开一批工具调用和结果，整批一起纳入摘要。 |
| 用户执行 `/compact` | 已保存且尚未压缩的原文超过 `agent.compaction_recent_tokens`，并且存在可整理的较早轮次 | 按完整用户轮次保留近期历史。即使最近一轮单独超出保留预算，也保留该轮。 |

Runner 的消息预算为 `agent.context_window_tokens − provider.max_tokens − 本次可用工具定义的估算 token 数`。系统提示词计入消息预算。触发压缩后，再扣除系统提示词，为摘要分配剩余额度的约四分之一，上限为 1,024 token，其余留给近期原文。生成后，Runner 用**实际摘要文本**重新估算整个请求。这里使用本地估算器；业务响应返回的 usage 用于统计。

日常任务的触发条件是本次请求的大小。`agent.compaction_threshold_tokens` 供 `SessionCompactor.compact()` 的阈值判断使用，AgentLoop 在任务结束后没有后台压缩阶段。[预算估算](../nanobot/session/tokens.py) · [手动压缩入口](../nanobot/agent/commands.py)

## 摘要如何生成和验证

`SessionCompactor` 把已有摘要和这次新增的历史前缀交给非流式 `LLMProvider.complete(..., response_schema=...)`。每批都为输入消息、Schema 和摘要输出预留预算。单条工具结果过长时会分片，每片沿用同一个原始索引；上一批的摘要作为下一批的输入。旧摘要格式或索引无效，或者需要在相同边界重新缩短摘要时，压缩器从该前缀的原文重新生成。

摘要有四个必填数组。下面是假设索引 2 是用户确认的缓存策略时，一份最小的有效摘要。

```json
{
  "user_constraints": [],
  "confirmed_decisions": [
    {"text": "数据库事务提交后，再删除 Redis 缓存。", "message_index": 2}
  ],
  "key_files": [],
  "unfinished_tasks": []
}
```

`provider.response_schema_mode` 决定适配器怎样请求这个结构。支持原生 Schema 的端点使用 `json_schema`；DeepSeek 官方 OpenAI 兼容端点默认使用 `json_object`，并将 Schema 说明追加到系统提示词；仅接受文本提示的端点可以配置 `prompt`。所有模式都经过本地校验：每项必须有非空 `text` 和整数 `message_index`，索引只能来自本批原文或上一份摘要已经引用的位置。索引验证通过意味着能定位来源，结论是否准确仍需对照原文。[摘要实现](../nanobot/session/compactor.py) · [Provider 配置示例](../.nanobot/nanobot.example.json)

## 轮内如何核对引用

假设磁盘已有索引 0～9。本轮用户消息为 10，助手发起的工具调用为 11，工具结果为 12。工具结果触发压缩后，模型可以根据摘要中的引用调用：

```python
read_session(session_id="@current", message_index=12, limit=1)
```

主 Agent 在每个工具批次开始前绑定一份原文快照。快照与压缩器共用相同的索引，覆盖已保存的历史以及本轮此前的消息。因此本轮尚未保存时，也能核对索引 12。同一批工具看到同一份快照，读取结果中的 `revision` 为 `null`。精确索引可以读取可见工具结果的正文，单条最多返回 1,000 个字符；相邻消息只返回可见的用户和助手正文。跨会话搜索命中则通过真实会话 ID 和文件版本读取。[会话读取](../nanobot/session/history.py) · [轮内引用回归用例](../tests/agent/test_run_context.py)

## 哪些数据会保存

- **完整原文**随轮次结果原子写入 Session JSONL。系统提示词和供模型使用的合成摘要消息不占原始消息索引。
- **可供下一轮复用的摘要**与 `summary_until` 写入 Session 头部。自动压缩只有在边界落在此前已保存的完整轮次之间或已保存历史末尾时，才产生可持久化的摘要。覆盖本轮工具结果的摘要只在当前运行中使用。`/compact` 成功后直接保存整理过的 Session。
- **失败或取消**时保留已保存的原文和摘要边界。如果模型返回截断、拒答、非法 JSON 或无效引用，或者压缩后的请求仍然超出预算，Runner 会结束本轮并报告上下文窗口错误。

工作区的 `memory/history.jsonl` 是长期记忆整理的事件队列，`.memory_cursor` 记录该队列的消费进度。排查某个会话为什么从索引 8 开始发送原文，应查看该 Session 头部的 `summary_until`。[轮次保存](../nanobot/agent/loop.py) · [Session 数据模型](../nanobot/session/models.py)
