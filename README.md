# 街道环卫考核 API

Django REST Framework + Pillow(pHash) + PostgreSQL/PostGIS 实现的纯 API 服务（无界面）。

## 设计原则（对应考核规则）

| 考核要求 | 实现方式 |
| --- | --- |
| 同一现场问题不能多次扣分 | 一个事件 `ProblemEvent` 对应唯一处罚单元 `PenaltyUnit`（1:1）；不同角度照片经**人工判定**后挂接同一事件，不新增扣分 |
| 相似照片不能自动合并不同地点 | pHash 只生成 `DuplicateCandidate`（pending 候选）；**候选生成阶段刻意不用位置/时间**；位置、时间只在人工 `decide` 时作为依据。误传同图可判 `different` 后分别立案 |
| 整改后复发是新事件 | 整改后复发照片判定 `create_new` → 新建事件、新建处罚单元；候选标记为 `recurrence`；向已整改事件挂接会被拒绝（400） |
| 扣分归属按发生时合同 | 立案时按 `occurred_at ∈ [valid_from, valid_to)` 且点在网格内解析合同，快照到事件/处罚单；与录入时间无关。无合同 422、区间重叠 409 |
| 逾期升级基于可注入时钟 | `POST /api/escalations/run/` body 传 `now`（或服务层传 `Clock`）；`FixedClock`/`OffsetClock` 支持重放，同刻重放幂等 |
| 复核锁定、更正只能追加 | 复核通过把 `locked_version` 指向当前版本；更正/升级一律 `PenaltyVersion` append-only，历史行不可改，追加后重新待复核 |
| 每笔扣分可追溯 | `penalty_no` 唯一 → 事件、责任合同/承包商、版本链、升级记录、复核记录、全部证据照片（含经纬度/拍摄时间/pHash） |
| 证据保全 | 照片只可上传/查询，不提供修改、删除（405）；处罚/版本只读 + 专用动作端点 |
| 离线采集乱序/重复回传 | 纯 API 采集包接入：操作带稳定操作号+设备内序号+业务发生时间+照片引用；服务端保存**不可变接收账本**、**设备水位**、**逐条回执**；重放返回原结果，缺失前序的操作待处理，绝不按到达顺序篡改事件 |
| 媒体上传中断 | 媒体按 `media_id` 幂等落盘可安全重传；证据照片/候选/事件/处罚在操作执行时**同一事务**物化，不留半个业务结果 |

pHash：Pillow 实现的 64 位 DCT 感知哈希（`assessment/services/phash.py`，仅依赖 Pillow）。

## 快速启动

### 方式 A：docker compose（PostGIS 镜像）

```bash
docker compose up --build
# OpenAPI: http://localhost:8000/api/schema/
```

### 方式 B：本地用户态（无 root，脚本自动装 PostgreSQL+PostGIS+GDAL）

```bash
./run_local.sh        # 起库、迁移、生成模拟图片、runserver
python manage.py seed_demo   # 另开终端：写演示网格/合同/照片
```

### 方式 C：已有 PostgreSQL/PostGIS

```bash
pip install -r requirements.txt
export DB_HOST=... DB_PORT=5432 DB_NAME=... DB_USER=... DB_PASSWORD=...
python manage.py migrate
python manage.py generate_mock_images   # 模拟图片到 media/mock_images/，并打印 pHash 距离矩阵
python manage.py runserver
```

## OpenAPI

* 在线：`GET /api/schema/`（JSON；加 `?format=yaml` 得 YAML）
* 静态导出：[`docs/openapi.json`](docs/openapi.json)、[`docs/openapi.yml`](docs/openapi.yml)
* 重新导出：`python manage.py spectacular --file docs/openapi.yml`

## 主要端点

| 方法 路径 | 说明 |
| --- | --- |
| `POST /api/photos/` | multipart 上传：`image` + `lng/lat` + `captured_at`；服务端算 pHash 并生成疑似候选 |
| `GET /api/candidates/?status=pending` | 疑似重复候选（含双方位置、拍摄时间、汉明距离） |
| `POST /api/candidates/{id}/decide/` | `attach`（同问题挂接，不扣分）/ `create_new`（复发或不同，另立案）/ `different` / `rejected` |
| `POST /api/photos/{id}/create_event/` | 对照片直接立案（无候选或判 different 后） |
| `POST /api/events/{id}/rectify/` | 整改回调（可注入 `now`）；重复回调 409 |
| `POST /api/escalations/run/` | 逾期扫描（可注入 `now`），返回新建升级数；幂等 |
| `POST /api/penalties/{id}/review/` | 复核，`approved=true` 锁定当前版本 |
| `POST /api/penalties/{id}/correct/` | 人工更正：只追加一个 correction 版本 |
| `GET /api/penalties/{id}/` | 完整追溯：事件、承包商、版本链、升级、复核、证据 |
| `/api/grids/` `/api/contracts/` `/api/events/` `/api/penalty-versions/` `/api/rectifications/` | 基础数据只读/维护 |
| `POST /api/ingest/media/` | 采集媒体暂存：`media_id`+`device_id`+`image`，按 media_id 幂等，中断可安全重传 |
| `POST /api/ingest/batches/` | 采集包批量接入：`device_id` + 操作数组（操作号/设备内序号/业务发生时间/照片引用），返回逐条回执 |
| `GET /api/ingest/operations/?device_id=&receipt__status=` | 账本与回执状态查询 |
| `POST /api/ingest/operations/{id}/retry/` | 人工重试 rejected/failed 操作（已 processed 返回 409） |
| `GET /api/ingest/devices/{device_id}/` | 设备水位：连续接收/处理序号与各状态计数 |

## 离线采集包接入（巡查员设备）

设备离线采集问题照片、立案资料和整改回执，联网后可能乱序、重复回传。接入流程：

```
1. 媒体先行    POST /api/ingest/media/     # 照片按 media_id 幂等落盘，可断点重传
2. 批量回传    POST /api/ingest/batches/   # 落账（幂等）→ 按设备内序号执行 → 逐条回执
3. 状态查询    GET  /api/ingest/operations/?device_id=PAD-001
4. 失败重试    POST /api/ingest/operations/{id}/retry/
```

操作类型与 payload 约定：

| op_type | payload | 执行效果 |
| --- | --- | --- |
| `report` | `{lng, lat, photo_refs[], category?, description?, note?}` | 物化证据照片 → 生成疑似候选 → 立案（按 `occurred_at` 归属合同）→ 处罚单元；同包多照片挂同一事件只扣一次 |
| `rectify` | `{event_no \| report_operation_no, note?, photo_refs[]?, lng?, lat?}` | 提交整改（`submitted_at` 取 `occurred_at`）；已整改事件的迟到回执记 `rejected`，不反转结案 |

回执状态机：`pending`（等前序/等媒体）→ `processed`；领域规则拒绝记 `rejected`
（终态、不阻塞后续、修复数据后可重试）；未预期错误记 `failed`（阻塞该设备后续、可重试）。
重放同一 `(device_id, operation_no)` 且内容一致 → 返回原回执；内容不一致或序号被占 → 409。
接入产生的事件/照片与直接上传完全同构，照常进入人工去重、发生时合同归属、整改与锁定边界。

## 典型流程（三个关键例子）

模拟图片（`python manage.py generate_mock_images`）及 pHash 距离：

```
scene_a_angle1（首报）
scene_a_angle2    距离 0   —— 不同角度
scene_a_repost    距离 2   —— 整改后复发
scene_a_elsewhere_copy 距离 0 —— 与首报逐像素相同，但坐标在 3km 外
scene_c_bins      距离 28  —— 明显不同现场（不产生候选）
```

1. **同图跨地点误传**：上传远处同图 → 出现候选 → 监督员核对坐标 `121.51,31.26` 与首报不符 →
   `decide=different` → 现场核实属实后 `create_event` → 归远处网格的丙公司，独立处罚单号。
2. **同地点复发**：首报事件 `rectify` → 复发照上传 → 候选 `decide=create_new`
   → 候选变 `recurrence`、产生第二事件与第二处罚单；向已整改事件 attach 会被拒绝。
3. **重复整改回调**：对同一事件第二次 `rectify` → 409，扣分不变。

## 测试

```bash
python manage.py test assessment -v 2
```

12 个用例（真实 PostGIS 测试库，迁移自动 `CREATE EXTENSION postgis`）：
pHash 距离、完整业务流（误传/复发/挂接/重复整改/历史归属/无合同/证据保全/追溯）、
注入时钟升级 + 复核锁定 + 追加更正、合同重叠 409、OpenAPI schema，
以及采集包接入（重复包幂等、乱序补齐后按序执行、媒体中断重传无孤儿、
迟到整改/候选判定不反转结案与锁定、拒绝后重试仍按发生时归属、新旧链路共存）。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚单元/版本/升级/复核
                                # + 设备水位/接收账本(不可变)/逐条回执/媒体暂存
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    ingest.py                   # 采集包接入：批量落账、按序执行、媒体暂存、重试
    clock.py                    # SystemClock / FixedClock / OffsetClock
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo
  tests/test_api.py             # 既有业务端到端测试
  tests/test_ingest.py          # 采集包接入端到端测试
docs/openapi.{json,yml}
```
