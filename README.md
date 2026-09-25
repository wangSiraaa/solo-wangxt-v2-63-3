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
| 离线采集乱序/重复回传 | 采集包接入：不可变接收账本（`op_id`/`seq` 双唯一）+ 设备水位 + 逐条回执；重放返回原结果；缺失前序待处理，补齐后按序号执行 |
| 媒体上传可安全重试 | 暂存媒体按 `media_key` 幂等；单条操作的业务写入（证据/事件/处罚/候选）同事务，失败整体回滚可重试 |

pHash：Pillow 实现的 64 位 DCT 感知哈希（`assessment/services/phash.py`，仅依赖 Pillow）。

## 离线采集包接入（/api/ingest/）

巡查员设备离线采集、联网后批量回传的接入层，纯 API：

1. **上传暂存媒体** `POST /api/ingest/media/`（multipart：`device_no` + `media_key` + `image`）。
   按 `(设备, media_key)` 幂等：上传中断后重传返回原记录（200），同键不同内容 409。
   暂存媒体不产生证据/候选，只有被操作成功消费才转为 `EvidencePhoto`。
2. **批量上送操作** `POST /api/ingest/batches/`：

```json
{
  "device_no": "DEV-001",
  "batch_no": "B20260925-01",
  "operations": [
    {"op_id": "01J9Z...", "seq": 1, "type": "photo_report",
     "occurred_at": "2026-09-25T08:30:00+08:00",
     "payload": {"media_key": "m-001", "category": "litter",
                 "lng": 121.475, "lat": 31.235, "uploader": "巡查员-张"}},
    {"op_id": "01J9Z...", "seq": 2, "type": "rectification",
     "occurred_at": "2026-09-25T11:00:00+08:00",
     "payload": {"report_op_id": "01J9Z...", "media_key": "m-002", "note": "已清理"}}
  ]
}
```

   * `photo_report`：问题照片立案 —— 媒体转证据、按**业务发生时间**归属合同、建事件/处罚、生成人工去重候选；
   * `rectification`：整改回执 —— `event_no` 或 `report_op_id`（引用本设备立案操作）定位事件，
     整改提交时间取业务发生时间；迟到的重复整改回执标记 `ignored`，不反转已结案事件。
3. **状态查询** `GET /api/ingest/devices/{device_no}/`（接收/处理水位 + 各状态计数）、
   `GET /api/ingest/receipts/?operation__device__device_no=...&status=...`（逐条回执，含账本原文）。
4. **失败重试** `POST /api/ingest/receipts/{id}/retry/`：仅 `failed` 可重试（其余 409）；
   成功后从设备水位处继续按序级联执行。

语义保证：

* 同一 `(设备, op_id)` 重放返回原回执（原结果快照）；同号不同内容 / 序号复用判 `conflict`，账本不变；
* 设备内序号从 1 连续递增；缺失前序的操作保持 `pending`，补齐后按序号顺序执行，不按到达顺序执行；
* 单条操作执行原子化：失败不留半个事件、处罚或相似照片候选，媒体不被误消费；
* 接入层复用既有领域服务：人工去重、发生时合同归属、整改、复核锁定边界完全一致；
  旧 `POST /api/photos/` 直传接口与既有数据不受影响。


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
| `POST /api/ingest/media/` | 暂存媒体上传（按 `media_key` 幂等，中断可重传） |
| `POST /api/ingest/batches/` | 采集包批量接入：逐条入账、按序执行、逐条回执 |
| `GET /api/ingest/devices/{device_no}/` | 设备接收/处理水位与各状态回执计数 |
| `GET /api/ingest/receipts/` | 逐条回执查询（含账本原文，可按设备/状态/批次过滤） |
| `POST /api/ingest/receipts/{id}/retry/` | 重试失败回执，成功后按序级联执行后续 |

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

13 个用例（真实 PostGIS 测试库，迁移自动 `CREATE EXTENSION postgis`）：
pHash 距离、完整业务流（误传/复发/挂接/重复整改/历史归属/无合同/证据保全/追溯）、
注入时钟升级 + 复核锁定 + 追加更正、合同重叠 409、OpenAPI schema；
采集包接入（重复包重放、序号 1/3/2 乱序补齐、媒体中断重传与失败回滚、
迟到整改/候选判定不反转结案与锁定、旧接口兼容、整改回执引用与业务发生时间）。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚单元/版本/升级/复核
                                #   + 采集设备/暂存媒体/接收账本/操作回执
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    ingest.py                   # 采集包接入：媒体暂存/批量接收/按序级联/重试
    clock.py                    # SystemClock / FixedClock / OffsetClock
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo
  tests/test_api.py             # 端到端测试（既有业务流）
  tests/test_ingest.py          # 端到端测试（采集包接入验收）
docs/openapi.{json,yml}
```
