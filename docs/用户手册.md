# LightRoute 轻途 用户使用手册

## 1. 如何安装

Windows PowerShell:

```powershell
cd D:\code\python\Traveler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS / Linux:

```bash
cd /path/to/Traveler
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 2. 如何配置

Windows PowerShell:

```powershell
$env:LLM_API_KEY="your-llm-api-key"
$env:LLM_MODEL_NAME="your-model-name"
$env:LLM_BASE_URL="https://your-llm-endpoint/v1"
$env:AMAP_WEB_SERVICE_KEY="your-amap-web-service-key"
```

macOS / Linux:

```bash
export LLM_API_KEY="your-llm-api-key"
export LLM_MODEL_NAME="your-model-name"
export LLM_BASE_URL="https://your-llm-endpoint/v1"
export AMAP_WEB_SERVICE_KEY="your-amap-web-service-key"
```

不要把真实 key 写入 README、截图、视频或公开仓库。

## 3. 如何启动

```powershell
python cli.py
```

健康检查：

```powershell
python cli.py health
```

启动后 CLI 会要求输入用户 ID。不同用户 ID 使用不同长期记忆文件。

## 4. 如何登录或配置用户

LightRoute 轻途当前没有 Web 登录系统。CLI 启动时输入用户 ID 即可：

```text
用户ID: demo_user
```

长期记忆会保存在：

```text
data/memory/demo_user.json
```

可用于跨会话保存偏好、历史聊天和历史行程。

## 5. 主要功能怎么用

### 5.1 规划路线

直接输入自然语言：

```text
北京短途游，从国贸出发，6小时，想吃本地特色，不想排队
```

系统会让你选择路线偏好：

```text
1. 打卡路线
2. 美食路线
3. 景点和餐饮兼顾
4. 跳过，由系统自动判断
```

如果输入里没有明确起点，系统会要求补充初始地：

```text
请输入初始地: 国贸
```

### 5.2 查看帮助

```text
help
```

### 5.3 查看当前状态

```text
status
```

### 5.4 查看历史行程

```text
history
```

### 5.5 查看偏好

```text
preferences
```

### 5.6 清空当前短期记忆

```text
clear
```

### 5.7 执行中修改或取消

路线规划执行中可以输入：

```text
/edit
```

重新编辑当前需求。

```text
/cancel
```

取消当前规划。

## 6. 示例操作流程

### 示例：北京美食短途

1. 启动 CLI：

```powershell
python cli.py
```

2. 输入用户 ID：

```text
用户ID: demo_user
```

3. 输入需求：

```text
北京短途游，从国贸出发，6小时，想吃本地特色，不想排队
```

4. 选择偏好：

```text
2
```

5. 等待系统展示：

```text
正在识别你的出行意图...
正在调度工具和智能体...
正在检索真实地点...
正在优化路线...
```

6. 查看输出：

```text
北京智能路线规划
短时路线安排
Route Options
注意事项
```

## 7. 推荐 CLI 示例输入

```text
北京短途游，从国贸出发，6小时，想吃本地特色，不想排队
```

```text
北京短途游，从国贸出发，6小时，想多拍照打卡，少排队
```

```text
我在天安门，想要进行3小时的citywalk，请为我推荐一条轻松的路线
```

```text
北京，我一下班想去按摩放松，然后吃个夜宵，大概3小时
```

```text
下雨了，想和女朋友在北京约会，看看展览，再找个安静小酒馆，4小时
```

```text
今天下午无事可做，和闺蜜想去做指甲和点小酒，大概5小时行程
```

## 8. 常见问题

### Q1: 系统提示缺少初始地怎么办？

输入更具体的起点，例如：

```text
国贸
```

不要只输入“北京”，因为路线矩阵需要具体坐标。

### Q2: AMap 请求失败怎么办？

检查：

```powershell
$env:AMAP_WEB_SERVICE_KEY
```

确认 key 有 Web 服务权限，并且网络可以访问 AMap。

### Q3: LLM 服务不可用怎么办？

运行：

```powershell
python cli.py health
```

确认 `LLM_API_KEY`、`LLM_MODEL_NAME`、`LLM_BASE_URL` 配置正确。

### Q4: 为什么有 warning？

warning 是系统主动暴露的不确定性，例如天气查询失败、营业时间不明、路径矩阵失败、活动槽候选不足。出现 warning 不代表程序崩溃，而是提醒用户出发前确认。

### Q5: 为什么没有生成路线？

常见原因：

- 缺少城市或起点坐标。
- 必需活动槽没有找到合格 POI。
- AMap 路径矩阵失败且启用了严格模式。
- 所有候选路线都超过用户时间预算。

## 9. 异常情况处理

| 异常 | 处理方式 |
| --- | --- |
| `missing_start_location_coordinates` | 输入更具体的起点，如商圈、地标、地铁站。 |
| `missing_destination_city` | 在需求中写明城市，如“北京”。 |
| `required_activity_slot_empty` | 换一种描述，或放宽活动要求。 |
| `route_cost_matrix_failed` | 检查 AMap key、网络、Web 服务权限。 |
| `no_route_within_time_budget` | 增加时长，或减少活动数量。 |
| LLM 熔断 | 等待恢复后重试，或运行健康检查。 |
