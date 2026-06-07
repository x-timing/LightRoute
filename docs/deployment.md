# LightRoute 轻途 部署文档

## 1. 环境要求

- Python 3.11+ 推荐。
- 可访问 LLM API。
- 可访问 AMap Web Service。
- 可选访问 wttr.in 天气服务。
- Windows PowerShell、Linux shell 或服务器终端。

## 2. 本地部署步骤

Windows PowerShell:

```powershell
cd D:\code\python\Traveler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:LLM_API_KEY="your-llm-api-key"
$env:LLM_MODEL_NAME="your-model-name"
$env:LLM_BASE_URL="https://your-llm-endpoint/v1"
$env:AMAP_WEB_SERVICE_KEY="your-amap-web-service-key"
python cli.py
```

## 3. 服务器部署步骤

Linux:

```bash
cd /data/app
git clone <your-repo-url> LightRoute
cd LightRoute
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export LLM_API_KEY="your-llm-api-key"
export LLM_MODEL_NAME="your-model-name"
export LLM_BASE_URL="https://your-llm-endpoint/v1"
export AMAP_WEB_SERVICE_KEY="your-amap-web-service-key"
python cli.py
```

后台运行示例：

```bash
cd /data/app/Traveler
source .venv/bin/activate
mkdir -p outputs
nohup python cli.py > outputs/traveler-cli.log 2>&1 &
```

查看进程：

```bash
ps -ef | grep "python cli.py" | grep -v grep
```

停止：

```bash
pkill -f "python cli.py"
```

## 4. 环境变量说明

| 环境变量 | 必需 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | LLM 服务 key。 |
| `LLM_MODEL_NAME` | 是 | 使用的模型名称。 |
| `LLM_BASE_URL` | 是 | OpenAI-compatible LLM API endpoint。 |
| `AMAP_WEB_SERVICE_KEY` | 是 | AMap Web Service key，用于 POI、地理编码、路线。 |
| `AMAP_KEY` | 可选 | AMap key 兼容变量。 |
| `AMAP_BASE_URL` | 可选 | 默认 `https://restapi.amap.com`。 |
| `AMAP_TIMEOUT_SEC` | 可选 | AMap 请求超时时间，默认 5 秒。 |
| `TRAVELER_ENABLE_WEB_UGC` | 可选 | 设为 `1` 时启用 UGC web fallback。 |
| `TRAVELER_WEB_UGC_TIMEOUT` | 可选 | UGC web fallback 超时时间。 |
| `TRAVELER_WEB_UGC_MAX_RESULTS` | 可选 | UGC web fallback 最大搜索结果数。 |

## 5. AMap / LLM 配置说明

不要在文档、代码片段、视频、截图中展示任何真实 key。

推荐做法：

```bash
export LLM_API_KEY="your-llm-api-key"
export LLM_MODEL_NAME="your-model-name"
export LLM_BASE_URL="https://your-llm-endpoint/v1"
export AMAP_WEB_SERVICE_KEY="your-amap-web-service-key"
```

当前代码 `config.py` 保留了历史兼容回退逻辑。正式提交和部署前建议改为只从环境变量读取，避免密钥进入仓库。

## 6. 数据目录说明

```text
data/memory/                       # 用户长期记忆 JSON
data/ugc/mock_poi_reviews.json      # 本地 mock UGC 数据
data/ugc/web_ugc_cache.json         # 可选 web UGC 缓存
data/models/bge-small-zh-v1.5/      # 本地 embedding 模型
outputs/                            # 日志和检查输出
.claude/skills/ask-question/data/   # RAG skill 数据
```

## 7. 启动命令

交互式 CLI：

```bash
python cli.py
```

健康检查：

```bash
python cli.py health
```

## 8. 停止与更新方式

停止：

```bash
pkill -f "python cli.py"
```

更新：

```bash
cd /data/app/Traveler
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python cli.py health
```

Windows 更新：

```powershell
cd D:\code\python\Traveler
git pull
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python cli.py health
```

## 9. 日志输出位置

当前项目没有统一日志目录配置。推荐部署时将标准输出写入：

```text
outputs/traveler-cli.log
```

命令：

```bash
mkdir -p outputs
nohup python cli.py > outputs/traveler-cli.log 2>&1 &
```

纯 Python 检查输出也建议写入 `outputs/`：

```bash
mkdir -p outputs
python tests/run_beijing_short_trip_checks.py > outputs/beijing-short-trip-checks.log 2>&1
```

## 10. 常见部署问题

### AMap key 不生效

检查环境变量：

```bash
echo "$AMAP_WEB_SERVICE_KEY"
```

确认 key 具备 Web 服务权限。

### LLM 健康检查失败

运行：

```bash
python cli.py health
```

确认 `LLM_API_KEY`、`LLM_MODEL_NAME`、`LLM_BASE_URL` 正确。

### 路线规划返回 `route_cost_matrix_failed`

可能原因：

- AMap key 缺少路线 API 权限。
- AMap QPS 限制。
- 网络无法访问 `restapi.amap.com`。
- transit 路线缺少城市 code 或 API 返回无路径。

### POI 召回为空

可能原因：

- 城市缺失。
- 起点过于笼统。
- 活动槽太具体。
- AMap 搜索关键词没有返回结果。

### 天气不可用

天气服务失败不会直接阻塞路线规划。系统会返回 `weather_query_failed` warning，并使用中性天气上下文。

## 11. 纯 Python 验证命令

```bash
python tests/test_tool_registry.py
python tests/test_route_preference_modes.py
python tests/test_cli_route_preference_choice.py
python tests/test_poi_search_urban_activity_specs.py
python tests/test_route_planning_activity_sequence_snapshot.py
python tests/test_route_cost_matrix_failure_diagnostics_snapshot.py
python tests/run_beijing_short_trip_checks.py
python tests/run_urban_micro_trip_scenario_coverage.py
```
