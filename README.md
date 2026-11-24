# 电耗字段单位目录（FastAPI + SQLite）

旧厂电耗报表字段丢失单位后的入仓前单位补齐服务。所有写入、单位核算、冲突集合和入仓清单投影在**同一个 SQLite 事务**中生效；查询接口直接读取已生效的投影表，不在查询时临时求解。

- Swagger UI：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>
- 健康检查：`GET /health`

## 1. 启动

### Docker Compose

```bash
docker compose up --build
```

打开 <http://127.0.0.1:8000/docs>。SQLite 文件位于容器 `/data/catalog.db`，通过 Compose 命名卷 `gsb-unit-catalog-data` 持久化。

### 本地运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

不使用 Docker 时，默认数据库为 `./data/catalog.db`；可用环境变量 `DATABASE_URL` 覆盖。

## 2. 单位与公式规则

只接受五个单位（也接受 `dimensionless`、`kw`、`kwh`、`kw/h`、中文别名等输入，响应使用规范名称）：

| 规范单位 | 量纲 `(功率指数, 时间指数)` |
|---|---:|
| `无量纲` | `(0, 0)` |
| `kW` | `(1, 0)` |
| `h` | `(0, 1)` |
| `kWh` | `(1, 1)` |
| `kW/h` | `(1, -1)` |

公式结构固定为：

```text
结果字段 = 左字段  运算符  右字段
```

运算符只允许 `+`、`-`、`*`、`/`（也接受 `add`、`减`、`×`、`÷` 等别名）。

- 加法/减法：三个字段必须是同一单位。
- 乘法：量纲指数相加，例如 `kW * h -> kWh`。
- 除法：量纲指数相减，例如 `kWh / kW -> h`、`kW / h -> kW/h`。
- 不做 `1000`、倍率或标量换算。
- 同一字段可出现在多条公式中，公式结果字段也可被其他公式引用，允许循环引用。

## 3. 在 `/docs` 按批登记、更正和撤回

核心接口：

```http
POST /batches
```

同样暴露在：

```text
POST /api/v1/batches
POST /api/v1/catalog/batches
```

一个批次可以同时包含字段、已知单位、公式和撤回操作；任一步骤出现未知编号/非法结构会整体回滚。

示例：

```bash
curl -X POST http://127.0.0.1:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_no": "B-001",
    "note": "电耗基础证据",
    "fields": [
      {"name": "power", "display_name": "功率"},
      {"name": "hours", "display_name": "运行小时"},
      {"name": "energy", "display_name": "电量"}
    ],
    "known_units": [
      {"evidence_no": "KU-P", "field": "power", "unit": "kW"},
      {"evidence_no": "KU-T", "field": "hours", "unit": "h"}
    ],
    "formulas": [
      {
        "formula_no": "F-001",
        "result": "energy",
        "left": "power",
        "operator": "*",
        "right": "hours"
      }
    ]
  }'
```

也支持字符串公式：

```json
{
  "known_units": ["a=kW", "b=kW"],
  "formulas": ["F-1: x = a / y", "F-2: y = x / b"]
}
```

更正：在批次中传入已存在的 `evidence_no` 或 `formula_no`；也可使用：

```http
PUT /known-units/{evidence_no}
PUT /formulas/{formula_no}
PUT /fields/{id_or_current_name}
```

撤回：

```http
POST   /known-units/{evidence_no}/withdraw
POST   /formulas/{formula_no}/withdraw
DELETE /known-units/{evidence_no}
DELETE /formulas/{formula_no}
```

撤回已撤回对象按幂等成功处理；撤回不存在的编号返回 `404` 且不改变目录。

## 4. 查询与入仓清单

常用接口（均有根路径、`/api`、`/api/v1`、`/api/v1/catalog` 前缀）：

| 接口 | 说明 |
|---|---|
| `GET /fields/{name}/units` | 单字段当前仍可能使用的单位、是否唯一、所属连通组 |
| `GET /units?field={name}` | 同上的查询参数形式 |
| `GET /candidates` | 所有字段的生效候选单位，直接读 `field_effective` 表 |
| `GET /warehouse` | 入仓清单数组，仅含单位唯一确定的字段，直接读 `warehouse_entries` 表 |
| `GET /warehouse/manifest` | 带 `count` 的清单对象 |
| `GET /conflicts` | 当前所有无解连通组及其“不能同时成立”的证据集合 |
| `GET /catalog?include_withdrawn=true` | 原始字段、已知单位、公式目录，包含已撤回证据 |
| `GET /batches/{batch_id}` | 历史批次的生效核算结果 |

批次响应中的关键字段：

```json
{
  "status": "effective | conflict",
  "solvable": true,
  "recomputed_groups": [],
  "conflicts": [],
  "unsat_core": [],
  "warehouse": [],
  "warehouse_count": 0
}
```

### 撤回联动

例如：

```text
F-10: energy = power * hours
已知 power = kW, energy = kWh
```

可唯一推出 `hours = h`，三个字段均入仓。撤回 `energy = kWh` 后：

- `power` 仍唯一为 `kW`，保留在清单；
- `hours` 候选为 `["无量纲", "h"]`；
- `energy` 候选为 `["kW", "kWh"]`；
- `hours`、`energy` 在同一事务中从 `warehouse_entries` 删除。

## 5. 无解冲突集合

若约束无解，批次仍保存合法目录证据，并返回一个删除序下的极小冲突集合。例如：

```text
F-001: c = a × b
已知 a = kW, b = h, c = kW
```

`kW × h = kWh`，与 `c = kW` 冲突。返回的 `unsat_core` 为：

```json
[
  {"type": "formula", "id": "F-001"},
  {"type": "known_unit", "id": "KA"},
  {"type": "known_unit", "id": "KB"},
  {"type": "known_unit", "id": "KC"}
]
```

该集合中去掉任一条后，此子集变为可满足。冲突后仍可调用 `/catalog` 查原始目录、调用 `/conflicts` 查当前冲突，也可以更正或撤回证据。与冲突组不相连的其他公式连通组不受影响，其唯一字段仍在入仓清单中。

## 6. 候选单位是怎么算的

实现位于 `app/reasoning.py`：

1. 把每个单位表示成功率/时间量纲指数。
2. 为每种运算符预计算合法的 `(结果单位, 左单位, 右单位)` 三元组；每个运算符最多只有 25 个操作数组合。
3. 对每个公式连通组做广义弧一致性传播（GAC），删除没有公式元组支持的单位。
4. 若仍有分歧，只在该连通组内用最小候选域（MRV）回溯寻找一个可行赋值。
5. 对某字段的每个允许单位分别强制该单位并检查是否存在至少一个一致解，从而得到**全局可实现**的精确候选集合。

因此应用不会构造“全目录所有字段 × 五个单位”的笛卡尔积，也不会靠穷举所有字段搭配生成候选。测试中的穷举对照器只存在于测试代码。

### 局部重算

公式按“同处一条公式即相连”形成超图连通组：

- 写入前记录被触碰字段当前所在的组；
- 收集这些旧组的全部成员，防止公式撤回/更正后旧组分裂导致漏算；
- 只沿有效公式边做定向闭包遍历；
- 在闭包内重建当前连通组并只调用求解器核算这些组；
- 同一事务删除受影响字段的旧投影/清单行，再写入新投影/清单行；
- 未受影响组的 `reasoning_groups.version` 和 `warehouse_entries` 均保持不变。

## 7. 代码结构

```text
app/
  main.py        # FastAPI 路由、/docs、启动时投影修复
  schemas.py     # 请求模型、别名、表达式解析
  service.py     # 批次事务、连通组定位、局部重算、有效投影查询
  reasoning.py   # 单位约束传播、精确候选求解、冲突核心
  units.py       # 五种单位、别名、量纲和运算符
  models.py      # SQLite ORM 表
  database.py    # engine/session/SQLite PRAGMA
tests/           # API 测试和随机穷举对照测试
scripts/selftest.py
Dockerfile
docker-compose.yml
```

主要 SQLite 表：

- `fields`：字段目录；
- `known_units`：已知单位证据，保留编号、版本、生效/撤回状态；
- `formulas`：整数编号 `formula_no` 与展示编号 `F-001`，保留版本和撤回状态；
- `batches`：批次请求与核算结果；
- `reasoning_groups`：连通组状态和冲突核心；
- `field_effective`：查询直接读取的候选单位投影；
- `warehouse_entries`：查询直接读取的入仓清单投影。

## 8. 已执行的自测

在交付环境中实际执行：

```bash
PYTHONPATH=/tmp/pylibs:/workspace python3 -m pytest /workspace/tests -q
# 8 passed
```

测试覆盖：

- 基础 `kW * h -> kWh`；
- 两条循环公式互引：`x = a / y`、`y = x / b`；
- 无解冲突核心、`/catalog` 仍可查、独立组仍入仓；
- 撤回已知单位后候选扩大并从清单删除；
- 撤回公式后候选恢复为五个单位；
- 更正公式时只重算受影响连通组，其他组版本号不变；
- 非法撤回的原子性；
- `/health` 与 `/docs`。

可读自测脚本也已实际通过：

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. python3 scripts/selftest.py
# 14 项断言全部通过
```

另外用固定随机种子生成 3000 个小型公式组，将求解器候选与 `itertools.product` 穷举结果对照，结果为 `0` 失配。

还实际启动过 Uvicorn 并验证：

```text
GET /health       -> {"status":"ok"}
GET /docs         -> Swagger UI 页面
GET /openapi.json -> 正常生成路径
POST /batches     -> 字符串循环公式可解析并正确推断
```

## 9. 余项与限制

- 当前交付沙箱中没有 `docker` / `docker compose` 可执行文件，因此未能在这里实际执行 `docker compose build/up`；已校验 `docker-compose.yml` 的 YAML 语法、Dockerfile 启动命令和应用本身的 Uvicorn/HTTP 行为。目标机器具备 Docker Compose v2 时可直接 `docker compose up --build`。
- 冲突核心是删除序下的极小不可满足集合，不保证是所有可能核心中证据条数最少的一个；接口返回的集合一定不能同时成立。
- 求解器对每个字段的每个候选单位执行一次可行性搜索，并在组内使用 GAC + MRV 回溯。普通报表公式规模足够；若单个连通组包含大量高度耦合公式，后续可再加入量纲线性方程消元或增量传播来降低搜索量。连通组之间始终隔离，更正不会重算全库。
- 当前没有做用户鉴权；这是内网入仓前目录服务，如需跨团队暴露应放在鉴权网关后或补充认证。
