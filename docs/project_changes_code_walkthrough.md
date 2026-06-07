# Traveler 智能路线规划改造说明文档

本文档面向项目队友，说明当前 Traveler 项目相对原始版本新增/改造了什么、每个相关 Python 文件的职责是什么，以及整体是按什么思路改写的。

## 1. 一句话概括当前变化

原来的 Traveler 更偏“多智能体商旅助手”：能做意图识别、事项收集、偏好记忆、问答、普通行程生成。

现在我们在它的基础上新增了一条“真实地点 + UGC + 确定性路线优化”的路线规划链路，使它能够处理：

```text
杭州一日游，想吃好，不想排队，6小时
```

这类请求，并输出：

- 至少 3 个真实地点串联
- 餐饮 + 文化/娱乐两类覆盖
- 每站到达/离开时间
- 排队风险和建议停留
- 多方案对比：少排队、均衡、效率优先、体验优先

## 2. 相比原项目，我们主要改了什么

### 2.1 新增真实 POI 数据层

新增：

- `services/amap_client.py`

作用：

- 调用高德 Web Service POI 接口。
- 将高德返回字段标准化为统一 POI 结构。
- 从 `AMAP_KEY` 环境变量读取 Key，不在代码中硬编码。

意义：

- 原来行程里的地点更多依赖 LLM 生成，可能存在不真实、不稳定的问题。
- 现在路线规划先拿真实高德地点，再做优化，减少幻觉。

### 2.2 新增 UGC 智慧层

新增：

- `services/ugc_service.py`
- `data/ugc/mock_poi_reviews.json`

作用：

- 用本地 mock UGC 数据给 POI 补充排队、口碑、标签、价格等信号。
- 对未命中 mock UGC 的真实地点，使用启发式规则估计排队风险。

意义：

- 高德 POI 解决“地点真实”，UGC 解决“好不好吃、排不排队、适不适合”的体验问题。

### 2.3 新增路线评分与优化层

新增：

- `planning/scoring.py`
- `planning/route_optimizer.py`

作用：

- 独立于 LLM，确定性计算地点分和路线分。
- 组合多个 POI，枚举访问顺序，生成时间表和多方案。

意义：

- LLM 不直接编路线，路线由算法生成，可解释、可测试、可调参。

### 2.4 新增两个路线相关 Skill

新增：

- `.claude/skills/poi-search/script/agent.py`
- `.claude/skills/route-planning/script/agent.py`

作用：

- `poi_search`：调用高德获取真实地点，并用 UGCService 补充信息。
- `route_planning`：解析用户约束和偏好，调用 RouteOptimizer 生成多方案。

意义：

- 保持原项目的 Skill/Agent 架构，不把新能力硬塞进一个大函数里。

### 2.5 改造意图识别和编排链路

改动：

- `agents/intention_agent.py`
- `agents/orchestration_agent.py`
- `agents/lazy_agent_registry.py`

作用：

- 让路线规划请求稳定进入：

```text
event_collection -> preference/memory_query -> poi_search -> route_planning -> itinerary_planning
```

这条链路。

意义：

- 原来“行程规划”可能只调 `event_collection + itinerary_planning`。
- 现在只要识别为游玩/路线类需求，就会补齐真实地点检索和路线优化步骤。

### 2.6 改造行程包装和 CLI 展示

改动：

- `.claude/skills/plan-trip/script/agent.py`
- `cli.py`

作用：

- 如果前序已经生成结构化 `route_options`，`itinerary_planning` 优先使用结构化路线，不再让 LLM 自由替换地点。
- CLI 展示用户能理解的进度和结果，例如“正在检索真实地点”“正在优化路线”，而不是内部文件或 Skill 名。
- 时长、排队风险、路线多方案以更自然的方式展示。

意义：

- 保证最终展示内容和真实 POI/路线优化结果一致。
- 提升演示和用户体验。

## 3. 当前完整执行顺序

用户输入：

```text
杭州一日游，想吃好，不想排队，6小时
```

当前执行顺序：

```text
cli.py
  -> IntentionAgent
      识别路线规划意图
      规范化调度计划
  -> OrchestrationAgent
      按优先级调度 Skill
  -> event_collection
      提取城市、日期、时长、出发地、返回地等
  -> preference / memory_query
      读取用户偏好和历史信息
  -> poi_search
      调用高德检索餐饮和文化/娱乐真实地点
      使用 UGCService 补充排队和标签
  -> route_planning
      解析时间、预算、少排队、偏好等约束
      调用 RouteOptimizer 生成多方案路线
  -> itinerary_planning
      将 route_options 包装成可读行程
  -> cli.py
      展示处理进度、主路线、注意事项、多方案对比
```

## 4. 每个核心 Python 文件的作用

### 4.1 `cli.py`

角色：

- 项目命令行入口。
- 负责用户输入、调用意图识别、调用编排器、展示结果。

和路线规划相关的改动：

- 在 `process_query` 中拿到 `IntentionAgent` 结果后，调用 `_normalize_agent_schedule`，保证路线规划链路完整。
- `_display_agents_called` 不再展示技术化 Agent 名，而是展示“处理进度”。
- `_generate_human_response` 支持展示路线结果中的：
  - 每日活动
  - 餐食建议
  - 注意事项
  - 多方案对比
- `_format_duration` 把分钟转成“4小时37分钟”。
- `_format_queue_wait` 把数值排队风险转成“可能需要短暂等候，预计排队约10-20分钟”。

队友看代码时重点看：

- `process_query`
- `_generate_human_response`
- `_get_route_option_display_title`
- `_format_duration`
- `_format_queue_wait`

### 4.2 `agents/intention_agent.py`

角色：

- 意图识别智能体。
- 判断用户请求应该交给哪些 Skill 处理。

和路线规划相关的改动：

- 新增 `poi_search`、`route_planning` 到可调度 Skill。
- 新增 `_normalize_agent_schedule`，当请求是行程/路线规划时，自动补齐：

```text
event_collection -> poi_search -> route_planning -> itinerary_planning
```

- 新增 `_upgrade_fallback_if_needed`，当 LLM 意图识别失败但本地规则判断用户是在问路线时，仍然走路线规划链路。
- 新增 `_fallback_entities`，从文本中简单提取目的地城市。

为什么这样改：

- LLM 可能识别不稳定，尤其在额度异常或响应失败时。
- 路线规划是比赛核心能力，所以必须有稳定兜底，不能因为意图识别失败就退化成普通搜索。

### 4.3 `agents/orchestration_agent.py`

角色：

- 多智能体编排器。
- 根据 `agent_schedule` 按优先级执行 Skill。
- 把前序 Agent 的结果传给后续 Agent。

和路线规划相关的作用：

- Priority 1 可以并行执行事项收集、偏好读取等。
- `poi_search` 依赖 `event_collection` 的目的地。
- `route_planning` 依赖 `poi_search` 的地点候选。
- `itinerary_planning` 依赖 `route_planning` 的结构化路线。

队友看代码时重点看：

- `_execute_parallel_agents`
- `_execute_agent`
- `_aggregate_results`
- `_canonical_agent_name`

### 4.4 `agents/lazy_agent_registry.py`

角色：

- Skill 懒加载注册器。
- 运行时按需加载 `.claude/skills/.../script/agent.py`。

和路线规划相关的改动：

- 新增 Skill 名映射：
  - `poi_search -> poi-search`
  - `route_planning -> route-planning`
- 新增用户友好进度提示：
  - `poi_search`：正在检索真实地点
  - `route_planning`：正在优化路线
  - `itinerary_planning`：正在生成行程方案

为什么这样改：

- 保持原项目“按需加载 Skill”的结构。
- CLI 不直接暴露内部文件名，演示时更自然。

### 4.5 `.claude/skills/event-collection/script/agent.py`

角色：

- 事项收集 Agent。
- 从用户输入中提取出发地、目的地、日期、时长、返回地、出行目的等。

和路线规划相关的改动：

- 增加本地规则 fallback。
- 当 LLM 不可用时，也能从“杭州一日游，6小时”中提取：
  - `destination=杭州`
  - `duration_days=1`
  - `trip_purpose=旅游`
  - `fallback_used=True`

为什么这样改：

- 真实演示时模型可能限流或失败。
- 事项收集是后续高德检索的前置条件，如果这里失败，整条路线链路会断。

### 4.6 `.claude/skills/poi-search/script/agent.py`

角色：

- 路线规划新增的 POI 检索 Skill。
- 不调用 LLM，属于工具型 Agent。

执行逻辑：

1. 从 `event_collection` 或 `context.key_entities` 中取目的地城市。
2. 调用 `AmapClient.search_text` 检索餐饮。
3. 调用 `AmapClient.search_text` 检索文化/娱乐。
4. 过滤低价值连锁快餐。
5. 对地点去重。
6. 调用 `UGCService.enrich_pois` 补充 UGC 信号。
7. 输出统一的 POI 列表和类别统计。

核心输出：

- `poi_search_complete`
- `city`
- `pois`
- `poi_counts`
- `sources=["amap", "mock_ugc"]`
- `warnings`

为什么这样设计：

- 高德调用、UGC 补充、去重和过滤都放在这个 Skill，后续路线优化只关心结构化 POI，不关心数据来源细节。

### 4.7 `.claude/skills/route-planning/script/agent.py`

角色：

- 路线规划 Skill。
- 不调用 LLM，负责把用户约束转成优化器参数，并输出结构化多路线。

执行逻辑：

1. 从前序结果中读取 `poi_search` 的 `pois`。
2. 从 `event_collection` 和用户文本中解析约束：
   - 总时长
   - 开始时间
   - 预算
   - 至少 3 个地点
   - 至少 1 个餐饮
   - 至少 2 个文化/娱乐
3. 从用户文本和偏好中解析偏好：
   - 少排队
   - 杭帮菜/美食
   - 文化/博物馆
   - 室内
   - 少走路/效率优先
4. 根据偏好决定 profile 顺序。
5. 调用 `RouteOptimizer.optimize`。

核心输出：

- `route_planning_complete`
- `route_options`
- `constraints`
- `profiles`
- `low_queue_requested`
- `diagnostics`
- `warnings`

为什么这样设计：

- 让 Agent 只负责“理解约束 + 调用工具”，具体算法放在 `planning` 包中，便于测试和调参。

### 4.8 `.claude/skills/plan-trip/script/agent.py`

角色：

- 原有行程规划 Agent。
- 现在增加了结构化路线包装能力。

和路线规划相关的改动：

- 如果前序结果中存在 `route_planning.route_options`，直接调用 `_build_itinerary_from_route_options`。
- 不再优先让 LLM 自由生成路线，避免替换高德真实地点。
- 将结构化路线转成：
  - `title`
  - `duration`
  - `daily_plans`
  - `activities`
  - `meals`
  - `notes`
  - `route_options`

展示友好处理：

- `_format_duration`：分钟转“约1小时30分钟”。
- `_queue_wait_text`：把 high/medium/low 转成用户能理解的等候描述。
- `_activities_from_route`：为每个地点生成时间段、描述和交通信息。
- 第一站显示“行程起点”，不再显示“0分钟”。

为什么这样改：

- 保留原有 LLM 行程规划能力作为 fallback。
- 但路线规划主链路必须优先使用结构化 `route_options`，保证真实、可执行、可复现。

### 4.9 `services/amap_client.py`

角色：

- 高德 POI Client。

核心函数：

- `search_text`：按城市、关键词、类型搜索 POI。
- `search_around`：按坐标附近搜索 POI，当前预留能力。
- `_request`：统一发送请求、处理高德错误码。
- `_normalize_poi`：把高德字段转成内部标准 POI。
- `_infer_category`：把高德 type/typecode 转成 `dining`、`culture_entertainment`、`other`。

为什么单独拆出来：

- 让高德 API 细节不污染 Agent。
- 单元测试可以注入 fake session，不需要真实网络。

### 4.10 `services/ugc_service.py`

角色：

- 本地 UGC 洞察服务。

核心函数：

- `find_by_poi`：按 POI id、名称、alias 匹配 mock UGC。
- `enrich_poi`：给单个 POI 补充 UGC。
- `enrich_pois`：批量补充。
- `_heuristic_queue_risk`：未命中 UGC 时估计排队风险。
- `_heuristic_tags`：生成标签。
- `_heuristic_tip`：生成用户可读建议。

为什么单独拆出来：

- 后续如果替换成真实点评数据或网络搜索摘要，只需要替换这一层，不影响路线优化器。

### 4.11 `planning/scoring.py`

角色：

- 路线评分的基础函数库。
- 不依赖 Agent，不调用外部服务。

核心内容：

- `PROFILE_WEIGHTS`：不同路线 profile 的权重。
- `haversine_meters`：计算两个地点间直线距离。
- `estimate_travel_minutes`：估算交通时间。
- `queue_risk`：读取 POI 的排队风险。
- `poi_rating`：读取评分。
- `sentiment_score`：读取 UGC 情感分。
- `estimated_cost`：估算消费。
- `preference_match_score`：偏好标签匹配。
- `poi_score`：计算单个地点综合分。

为什么单独拆出来：

- 评分逻辑是算法核心，放在独立文件里方便测试、解释和调参。

### 4.12 `planning/route_optimizer.py`

角色：

- 路线优化器。
- 当前路线规划最核心的确定性算法模块。

核心流程：

1. `_valid_pois`：过滤没有坐标的 POI。
2. `_select_profile_candidates`：每类保留高分候选，控制搜索规模。
3. `_candidate_groups`：组合餐饮、文化/娱乐和其他地点，满足类别约束。
4. `_evaluate_profile`：对每组地点枚举访问顺序。
5. `_build_route`：计算距离、交通时间、停留时间、预算、排队风险、时间表。
6. `_route_score`：根据 profile 权重计算路线得分。
7. `_pick_diverse_route`：尽量让不同 profile 输出不同路线。
8. `_constraint_status`：标记是否满足地点数、类别覆盖、时间、预算。
9. `_route_warnings`：生成约束提醒。

默认停留时间：

- 餐饮：60 分钟
- 文化/娱乐：90 分钟
- 其他：45 分钟

为什么这样设计：

- 当前候选数量可控，组合 + 排列搜索简单可靠。
- 相比复杂路径规划算法，更适合比赛阶段快速稳定展示。
- 后续可以替换成更复杂的 TSP/OR-Tools/高德路径规划，但接口不需要大改。

## 5. 测试文件分别验证什么

### 5.1 `tests/test_poi_ugc_services.py`

验证：

- 高德 POI 返回结果能被正确标准化。
- around search 坐标格式正确。
- 没有 `AMAP_KEY` 时会报明确错误。
- 高德错误码会被转成异常。
- UGC 能按 id/name/alias 匹配并补充字段。
- 未命中 UGC 时能用启发式估计排队风险。

### 5.2 `tests/test_route_optimizer.py`

验证：

- 优化器能返回多 profile 路线。
- 少排队 profile 会优先选择低排队餐饮。
- 时间预算足够时可以加入第 4 个地点。
- 候选允许时，不同 profile 能返回差异化路线。
- 餐饮会尽量安排在午餐时间附近。
- 缺少必要类别时会返回 warning。

### 5.3 `tests/test_route_skill_agents.py`

验证：

- `event_collection` 在 LLM 失败时能本地 fallback。
- `poi_search` 能使用 fake 高德数据产出 POI。
- `route_planning` 能基于 POI 产出路线。
- `itinerary_planning` 能使用 route_options 生成最终行程。
- 整条 Skill 链路可以串起来。

### 5.4 `tests/test_route_orchestration_flow.py`

验证：

- `OrchestrationAgent` 能按完整路线规划流程调度各 Skill。
- 前序结果能正确传给后续 Skill。
- 最终能拿到带 `route_options` 的行程结果。

### 5.5 `tests/test_intention_schedule_normalization.py`

验证：

- 如果 LLM 只识别到普通 itinerary planning，也会被扩展成完整路线链路。
- 非行程类请求不会被错误改写。
- LLM 失败时，路线类请求能进入本地兜底规划链路。

### 5.6 `tests/smoke_amap_real.py`

验证：

- 使用真实 `AMAP_KEY` 能从高德拿到 POI。
- 高德 Key 类型和状态可用。

### 5.7 `tests/smoke_route_pipeline_real.py`

验证：

- 真实高德检索 + UGC 补充 + 路线优化 + 行程包装的完整链路可跑通。
- 这是最接近演示场景的 smoke test。

## 6. 按什么思路改写的

### 6.1 不让 LLM 直接决定路线

路线规划需要满足时间、距离、类别、预算、排队等硬约束。如果完全让 LLM 生成，容易出现：

- 地点不真实
- 时间安排不合理
- 餐饮和景点类别不满足
- 多方案只是文字差异，实际路线差异不明确

因此我们采用：

```text
LLM 负责理解和调度
高德负责真实地点
UGC 负责体验信号
优化器负责路线计算
行程 Agent 负责表达
```

### 6.2 保持原项目多 Agent 架构

我们没有把路线规划写成一个独立脚本，而是拆成 Skill：

- `poi_search`
- `route_planning`
- `itinerary_planning`

这样能和原有的 `event_collection`、`preference`、`memory_query`、`OrchestrationAgent` 保持一致。

### 6.3 工具型 Skill 尽量确定性

`poi_search` 和 `route_planning` 都不调用 LLM。

好处：

- 响应更稳定。
- 单元测试更容易。
- 演示时不完全依赖大模型额度。
- 算法结果可解释。

### 6.4 数据层和算法层分离

拆分方式：

- `services`：负责外部/本地数据。
- `planning`：负责评分和优化。
- `.claude/skills`：负责 Agent 输入输出和流程对接。
- `cli.py`：负责展示。

好处：

- 后续换 UGC 数据源，不影响优化器。
- 后续接高德路径规划 API，不影响 CLI。
- 后续调评分权重，不影响 Skill 调度。

### 6.5 先保证比赛硬约束

当前算法优先保证：

- 至少 3 个地点
- 餐饮 + 文化/娱乐覆盖
- 显示时间安排
- 多方案输出
- 少排队偏好可体现
- 真实高德地点可检索

在此基础上，再逐步优化体验和创新点。

## 7. 当前已经验证通过的命令

远程服务器上已通过：

```bash
python tests/test_poi_ugc_services.py
python tests/test_route_optimizer.py
python tests/test_route_skill_agents.py
python tests/test_route_orchestration_flow.py
python tests/test_intention_schedule_normalization.py

export AMAP_KEY="高德 Web Service Key"
python tests/smoke_route_pipeline_real.py
python cli.py
```

典型 CLI 输入：

```text
杭州一日游，想吃好，不想排队，6小时
```

典型输出包含：

- 处理进度
- 杭州智能路线规划
- 每个地点的时间段
- 建议停留
- 等候情况
- 主方案注意事项
- 少排队、均衡、效率优先、体验优先路线对比

## 8. 队友接手建议

如果要看主流程：

1. 先看 `cli.py::process_query`
2. 再看 `agents/intention_agent.py::_normalize_agent_schedule`
3. 再看 `agents/orchestration_agent.py::_execute_parallel_agents`
4. 再看 `.claude/skills/poi-search/script/agent.py`
5. 再看 `.claude/skills/route-planning/script/agent.py`
6. 最后看 `.claude/skills/plan-trip/script/agent.py::_build_itinerary_from_route_options`

如果要看算法：

1. 先看 `planning/scoring.py`
2. 再看 `planning/route_optimizer.py::optimize`
3. 重点看 `_candidate_groups`、`_build_route`、`_route_score`、`_build_schedule`

如果要看数据接入：

1. 先看 `services/amap_client.py`
2. 再看 `services/ugc_service.py`
3. 再看 `data/ugc/mock_poi_reviews.json`

如果要看测试：

1. `tests/test_route_optimizer.py`
2. `tests/test_route_skill_agents.py`
3. `tests/test_route_orchestration_flow.py`
4. `tests/smoke_route_pipeline_real.py`

## 9. 后续可继续做的增强

建议优先级：

1. 自然语言调整闭环：例如“换一家更近的餐厅”“不要西湖核心区”“预算降到150”。
2. 接入高德路径规划 API：把当前距离估算替换成真实步行/驾车时间。
3. 扩展 UGC 来源：从 mock UGC 扩展到网络搜索摘要或真实点评数据。
4. 前端/地图可视化：把路线画在地图上，更适合比赛展示。
5. 更细粒度偏好：亲子、情侣、雨天、室内、无障碍等。

