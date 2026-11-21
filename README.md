# 地勘取芯数据入仓演示（FastAPI + SQLite）

岩芯存在缺失时，样品孔深不能按岩芯实长直接累加：相邻岩芯段之间的缺失长度只给出
上下限，因此每个段、每个样品的孔深都是一个**可行范围**。本应用对取芯回次做约束
求解，只有在**所有可行排布中都完整落入同一地层**的样品才连同层位写进入仓 CSV，
其余样品仅展示起止孔深各自的可行范围与未入仓原因。

深度一律使用**整数毫米**，所有区间**左闭右开**。

## 一、模型与判定规则

设回次有 n 个岩芯段，段实长 L_j，段 j-1 与段 j 之间缺失 g_j ∈ [low_j, high_j]。
令 P_0=0、P_j = g_1+…+g_j，则：

- 段 j 起点孔深 = 回次起始 + P_j，终点孔深 = 起点 + L_j；
- 回次起止孔深之差必须容纳全部岩芯：P_{n-1} ≤ 回次进尺 − 实长合计
  （回次顶部/终孔处的剩余缺失为非负自由量，不要求填到终孔）；
- 数据员录入的“段起点实测孔深”等价于等式约束 P_j = 实测值 − 回次起始。

求解器对每个前缀 P_j 分别求全部可行排布下的最小值/最大值（线性规划的部分和紧边界，
分“等式块”和末端“开放块”处理），再换算为段与样品的起止孔深可行范围。

样品在段内位置 [a, b) 固定，故：

- 起点可行范围 = [段起点_lo + a, 段起点_hi + a]
- 终点可行范围 = [段终点_lo − (L−b), 段终点_hi − (L−b)]
- 样品可能占据的全部整数孔深为 [起点_lo, 终点_hi − 1]；该范围内没有地层分界时，
  层位在所有排布中唯一，样品连同层位写进入仓 CSV，否则暂不入仓并展示原因。

## 二、功能

- 导入取芯回次：回次起止孔深、按序岩芯段实长、段间缺失上下限、段内样品编号/位置/
  化验值、地层分界与层位名称（粘贴 JSON 或上传 .json 文件，页面附演示数据）。
- 详情页展示：各段起止孔深可行范围、每个样品的起止可行范围、入仓判定与原因、
  生效入仓清单、更正历史。
- 数据员录入/更正岩芯段起点实测孔深：提交后整份回次重新求解，**整份替换**供数表，
  版本号 +1。
- 约束无解或页面版本过期：明确提示原因，事务回滚，**当前供数原样保留**。
- 生效数据保存在 SQLite（`runs / measured_starts / supply_rows / correction_log`
  四张表）；入仓 CSV 为 utf-8-sig，Excel 可直接打开。

## 三、启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据库与数据目录保存在命名卷 `coring_data`（容器内 `/data`），重启不丢数据。
停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 同时删除数据卷
```

本地不使用 Docker 时也可直接运行（Python 3.11）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 四、自测

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

测试覆盖：开放块/等式块部分和紧边界、实测锚点收缩、左闭右开地层判定、无解与录入
校验、导入取数、更正整份更新、过期版本拒绝、无解回滚、CSV 内容、文件上传。

## 五、目录结构

```
app/
  engine.py            # 录入校验 + 可行区间求解 + 地层判定（纯函数，无框架依赖）
  storage.py           # SQLite 持久化、整份替换事务、入仓 CSV 生成
  main.py              # FastAPI 路由与 flash 提示
  templates/           # Jinja2 页面（列表/导入/详情）
  demo_payload.json    # 导入页预填演示回次
tests/                 # pytest 自测
Dockerfile, docker-compose.yml, requirements.txt
```

## 六、录入 JSON 字段

| 字段 | 含义 |
| --- | --- |
| `run_id` | 回次编号（主键） |
| `start_depth` / `end_depth` | 回次起止孔深，整数毫米，左闭右开 |
| `segments[].length` | 岩芯段实长（毫米，正整数，按取芯顺序） |
| `segments[].samples[]` | `sample_no`、段内 `start/end`（左闭右开）、`assay` 化验值 |
| `gaps[]` | 段间缺失 `low/high`（毫米），数量 = 段数 − 1 |
| `boundaries` | 地层分界深度（严格递增，位于回次区间内部） |
| `layers` | 层位名称，数量 = 分界数 + 1 |
| `measured_starts[]` | 可选，`seg_index`（从 0 起）与实测起点孔深 `depth` |
