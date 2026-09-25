"""
离线采集包接入服务。

巡查设备离线产生操作（问题上报 report / 整改回执 rectify），联网后批量回传。
规则：
* 接收账本不可变：同一 (设备, 操作号) 重放返回原回执；内容不一致或序号被占 -> 409；
* 严格按设备内序号执行：缺失前序（空洞）的操作保持待处理，
  绝不按到达顺序篡改业务事件；
* 媒体与账本分离：照片二进制先幂等落盘，证据照片/疑似候选/事件/处罚
  在操作被执行时同一事务物化——上传中断重传不会留下半个业务结果；
* 业务拒绝（领域异常）是终态、不阻塞后续操作，修复数据后可人工重试；
  未预期错误阻塞该设备后续操作，等待人工重试；
* 业务时间一律取操作携带的 occurred_at（合同归属、整改提交时间），与到达时间无关。
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import timezone as dt_timezone

from django.contrib.gis.geos import Point
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from assessment.exceptions import (
    AlreadyProcessed,
    DomainError,
    IngestConflict,
    IngestReferenceNotFound,
    InvalidMedia,
)
from assessment.models import (
    DeviceWatermark,
    EvidencePhoto,
    IngestMedia,
    IngestOperation,
    IngestReceipt,
    ProblemEvent,
)
from assessment.services.clock import FixedClock
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.events import create_event_from_photo
from assessment.services.phash import compute_phash_hex
from assessment.services.rectification import submit_rectification


class MediaNotReady(Exception):
    """内部信号：操作引用的媒体尚未上传——保持待处理，不视为失败。"""

    def __init__(self, media_id: str):
        self.media_id = media_id
        super().__init__(f"媒体 {media_id} 尚未上传")


TERMINAL_STATUSES = (IngestReceipt.Status.PROCESSED, IngestReceipt.Status.REJECTED)


@dataclass
class IngestBatchResult:
    device_id: str
    watermark: DeviceWatermark
    receipts: list = field(default_factory=list)  # [(IngestOperation, replayed)]


# ---------------------------------------------------------------------------
# 回执序列化辅助（视图层共用）
# ---------------------------------------------------------------------------

def receipt_to_dict(op: IngestOperation, *, replayed: bool = False) -> dict:
    receipt = op.receipt
    return {
        "operation_id": op.id,
        "operation_no": op.operation_no,
        "seq": op.seq,
        "op_type": op.op_type,
        "status": receipt.status,
        "replayed": replayed,
        "result": receipt.result,
        "error_code": receipt.error_code,
        "error_detail": receipt.error_detail,
        "status_note": receipt.status_note,
        "attempts": receipt.attempts,
        "received_at": op.received_at,
        "processed_at": receipt.processed_at,
    }


# ---------------------------------------------------------------------------
# 设备水位
# ---------------------------------------------------------------------------

def _lock_watermark(device_id: str) -> DeviceWatermark:
    """取设备水位行并加行锁（同一设备的接入/重试由此串行化）。"""
    wm, _ = DeviceWatermark.objects.select_for_update().get_or_create(device_id=device_id)
    return wm


def _refresh_watermarks(wm: DeviceWatermark) -> None:
    """从账本+回执重算两个水位（唯一权威算法，任何状态变化后调用）。"""
    statuses = dict(
        IngestReceipt.objects.filter(operation__device_id=wm.device_id)
        .values_list("operation__seq", "status")
    )
    received = 0
    while received + 1 in statuses:
        received += 1
    processed = 0
    while statuses.get(processed + 1) in TERMINAL_STATUSES:
        processed += 1
    wm.received_through = received
    wm.processed_through = processed
    wm.save(update_fields=["received_through", "processed_through", "updated_at"])


# ---------------------------------------------------------------------------
# 批量接入
# ---------------------------------------------------------------------------

def _payload_hash(op_type: str, seq: int, occurred_at, payload: dict) -> str:
    canonical = json.dumps(
        {
            "op_type": op_type,
            "seq": seq,
            # 归一到 UTC，避免同一时刻不同时区写法被误判为内容变更
            "occurred_at": occurred_at.astimezone(dt_timezone.utc).isoformat(),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@transaction.atomic
def ingest_batch(*, device_id: str, operations: list[dict]) -> IngestBatchResult:
    """
    批量接入：校验冲突 -> 落账（幂等）-> 推进水位 -> 按序执行 -> 返回逐条回执。
    同一批次允许同时包含新操作与重放操作；空批次等价于一次“触发处理”。
    """
    wm = _lock_watermark(device_id)

    # 第一遍：只做冲突检查，不落账（整批要么全部接收，要么 409）
    seen_seqs, seen_nos = set(), set()
    pending_items = []  # (item, digest)
    replayed_nos = set()
    for item in operations:
        op_no, seq = item["operation_no"], item["seq"]
        if op_no in seen_nos or seq in seen_seqs:
            raise IngestConflict(f"批次内操作号/序号重复：{op_no} / seq={seq}")
        seen_nos.add(op_no)
        seen_seqs.add(seq)
        digest = _payload_hash(item["op_type"], seq, item["occurred_at"], item["payload"])

        existing = IngestOperation.objects.filter(device_id=device_id, operation_no=op_no).first()
        if existing is not None:
            if existing.payload_hash != digest or existing.seq != seq:
                raise IngestConflict(f"操作号 {op_no} 已落账但内容不一致，疑似设备端重用操作号")
            replayed_nos.add(op_no)
            continue
        if IngestOperation.objects.filter(device_id=device_id, seq=seq).exists():
            raise IngestConflict(f"设备内序号 {seq} 已被其他操作占用")
        pending_items.append((item, digest))

    # 第二遍：落账（账本行 + 待处理回执）
    for item, digest in pending_items:
        op = IngestOperation.objects.create(
            device_id=device_id,
            operation_no=item["operation_no"],
            seq=item["seq"],
            op_type=item["op_type"],
            occurred_at=item["occurred_at"],
            payload=item["payload"],
            payload_hash=digest,
        )
        IngestReceipt.objects.create(operation=op)

    _refresh_watermarks(wm)
    _process_device_locked(wm)

    result = IngestBatchResult(device_id=device_id, watermark=wm)
    for item in operations:
        op = IngestOperation.objects.select_related("receipt").get(
            device_id=device_id, operation_no=item["operation_no"],
        )
        result.receipts.append((op, op.operation_no in replayed_nos))
    return result


# ---------------------------------------------------------------------------
# 按序执行
# ---------------------------------------------------------------------------

def _process_device_locked(wm: DeviceWatermark) -> None:
    """按设备内序号顺序执行待处理操作（调用方须已持有水位行锁）。"""
    while True:
        next_seq = wm.processed_through + 1
        op = (
            IngestOperation.objects.filter(device_id=wm.device_id, seq=next_seq)
            .select_related("receipt")
            .first()
        )
        if op is None or op.receipt.status != IngestReceipt.Status.PENDING:
            break
        receipt = op.receipt
        receipt.attempts += 1
        try:
            with transaction.atomic():
                # 单操作一个事务：任一环节失败，照片/候选/事件/处罚整体回滚
                outcome = _dispatch_operation(op)
                receipt.status = IngestReceipt.Status.PROCESSED
                receipt.result = outcome
                receipt.error_code = ""
                receipt.error_detail = ""
                receipt.status_note = ""
                receipt.processed_at = timezone.now()
                receipt.save()
        except MediaNotReady as exc:
            # 媒体未到位：保持待处理，等待媒体上传/重试唤醒
            receipt.status_note = f"等待媒体 {exc.media_id} 上传"
            receipt.save()
            break
        except DomainError as exc:
            # 业务规则拒绝：终态、不阻塞后续，可修复数据后人工重试
            receipt.status = IngestReceipt.Status.REJECTED
            receipt.error_code = type(exc).__name__
            receipt.error_detail = exc.detail
            receipt.status_note = "业务规则拒绝，可修复数据后重试"
            receipt.processed_at = timezone.now()
            receipt.save()
        except Exception as exc:  # noqa: BLE001 —— 未预期错误：阻塞后续，等待重试
            receipt.status = IngestReceipt.Status.FAILED
            receipt.error_code = type(exc).__name__
            receipt.error_detail = str(exc)[:512]
            receipt.status_note = "处理失败，该设备后续操作已暂停，请重试"
            receipt.processed_at = timezone.now()
            receipt.save()
            break
        wm.processed_through = next_seq  # 局部推进，统一由 _refresh_watermarks 落库
    _refresh_watermarks(wm)


def _dispatch_operation(op: IngestOperation) -> dict:
    if op.op_type == IngestOperation.OpType.REPORT:
        return _handle_report(op)
    if op.op_type == IngestOperation.OpType.RECTIFY:
        return _handle_rectify(op)
    raise IngestConflict(f"未知操作类型 {op.op_type}")


def _materialize_photos(op: IngestOperation, refs: list[str], *, lng: float, lat: float, note: str):
    """把暂存媒体物化为证据照片；任一媒体未上传 -> MediaNotReady（整体不落地）。"""
    photos = []
    for ref in refs:
        media = IngestMedia.objects.filter(media_id=ref).first()
        if media is None:
            raise MediaNotReady(ref)
        if media.photo_id is not None:
            # 处理是单操作事务，已物化只可能是被其他操作消费过
            raise IngestConflict(f"媒体 {ref} 已被其他操作使用，不能重复立案")
        photo = EvidencePhoto.objects.create(
            image=media.image.name,  # 复用已落盘文件，不重复存储
            phash=media.phash,
            captured_at=op.occurred_at,
            location=Point(lng, lat, srid=4326),
            uploader=op.device_id,
            note=note,
        )
        media.photo = photo
        media.save(update_fields=["photo"])
        photos.append(photo)
    return photos


def _handle_report(op: IngestOperation) -> dict:
    """问题上报：物化照片 -> 生成疑似候选 -> 立案（发生时合同归属）-> 处罚单元。"""
    payload = op.payload
    photos = _materialize_photos(
        op, payload["photo_refs"],
        lng=payload["lng"], lat=payload["lat"], note=payload.get("note", ""),
    )
    # 与直接上传一致：每张照片都进入既有的人工去重体系（只生成候选，不自动合并）
    for photo in photos:
        generate_candidates_for_photo(photo)

    event = create_event_from_photo(
        photos[0],
        category=payload.get("category", ProblemEvent.Category.OTHER),
        description=payload.get("description", ""),
        actor=f"device:{op.device_id}",
        dedup_key=f"ingest:{op.device_id}:{op.operation_no}",
        occurred_at=op.occurred_at,
    )
    # 同一次上报的其余照片挂到同一事件（同一现场只扣一次）
    for photo in photos[1:]:
        photo.event = event
        photo.save(update_fields=["event"])

    return {
        "event_id": event.id,
        "event_no": event.event_no,
        "penalty_no": event.penalty.penalty_no,
        "photo_ids": [p.id for p in photos],
    }


def _handle_rectify(op: IngestOperation) -> dict:
    """整改回执：解析事件 -> 物化整改照片 -> 提交整改（业务时间=occurred_at）。"""
    payload = op.payload
    event = _resolve_rectify_event(op)

    lng, lat = payload.get("lng"), payload.get("lat")
    if lng is None or lat is None:
        lng, lat = event.location.x, event.location.y
    photos = _materialize_photos(
        op, payload.get("photo_refs", []), lng=lng, lat=lat, note=payload.get("note", ""),
    )
    for photo in photos:
        generate_candidates_for_photo(photo)

    # 已整改事件再次回执 -> AlreadyRectified（领域异常），回执记为 rejected，状态不被反转
    rectification = submit_rectification(
        event,
        note=payload.get("note", ""),
        actor=f"device:{op.device_id}",
        photo=photos[0] if photos else None,
        clock=FixedClock(op.occurred_at),
    )
    for photo in photos:
        if photo.event_id is None:
            photo.event = event
            photo.save(update_fields=["event"])

    return {
        "event_id": event.id,
        "event_no": event.event_no,
        "rectification_id": rectification.id,
        "photo_ids": [p.id for p in photos],
    }


def _resolve_rectify_event(op: IngestOperation) -> ProblemEvent:
    payload = op.payload
    event_no = payload.get("event_no")
    ref_op_no = payload.get("report_operation_no")
    if event_no:
        event = ProblemEvent.objects.filter(event_no=event_no).first()
        if event is None:
            raise IngestReferenceNotFound(f"事件 {event_no} 不存在")
        return event
    if ref_op_no:
        # 引用本设备此前的上报操作；由于严格按序执行，被引用操作必然已终态
        ref = IngestOperation.objects.filter(
            device_id=op.device_id, operation_no=ref_op_no,
        ).first()
        if ref is None:
            raise IngestReferenceNotFound(f"引用的上报操作 {ref_op_no} 不存在")
        if ref.receipt.status != IngestReceipt.Status.PROCESSED:
            raise IngestReferenceNotFound(
                f"引用的上报操作 {ref_op_no} 未成功处理（当前 {ref.receipt.status}）"
            )
        return ProblemEvent.objects.get(pk=ref.receipt.result["event_id"])
    raise IngestReferenceNotFound("整改回执必须引用 event_no 或 report_operation_no")


# ---------------------------------------------------------------------------
# 媒体暂存（幂等，可安全重传）
# ---------------------------------------------------------------------------

@transaction.atomic
def store_media(*, device_id: str, media_id: str, image_file) -> tuple[IngestMedia, bool]:
    """
    媒体落盘：同一 media_id 重传相同内容返回原记录（reused）；
    内容或设备不一致 -> 409。落盘后触发一次设备处理循环，唤醒等待媒体的操作。
    """
    data = image_file.read()
    digest = hashlib.sha256(data).hexdigest()

    existing = IngestMedia.objects.filter(media_id=media_id).first()
    if existing is not None:
        if existing.sha256 == digest and existing.device_id == device_id:
            return existing, False
        raise IngestConflict(f"媒体号 {media_id} 已存在但内容不一致")

    try:
        phash = compute_phash_hex(ContentFile(data, name="probe"))
    except Exception as exc:
        raise InvalidMedia(f"媒体 {media_id} 无法解析为图片：{exc}") from exc

    original_name = getattr(image_file, "name", "") or ""
    suffix = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "png"
    media = IngestMedia(media_id=media_id, device_id=device_id, phash=phash, sha256=digest)
    media.image.save(f"{media_id}.{suffix}", ContentFile(data), save=False)
    try:
        with transaction.atomic():
            media.save()
    except IntegrityError:
        # 并发上传同一媒体号：内容一致则幂等复用，否则冲突
        existing = IngestMedia.objects.filter(media_id=media_id).first()
        if existing is not None and existing.sha256 == digest and existing.device_id == device_id:
            return existing, False
        raise IngestConflict(f"媒体号 {media_id} 已存在但内容不一致") from None

    wm = _lock_watermark(device_id)
    _process_device_locked(wm)
    return media, True


# ---------------------------------------------------------------------------
# 人工重试
# ---------------------------------------------------------------------------

@transaction.atomic
def retry_operation(operation_id: int) -> IngestReceipt:
    """
    重试单条操作：
    * processed —— 409（重放请走批量接入拿原回执，不能重复执行）；
    * pending   —— 触发一次设备处理循环（前序/媒体可能刚补齐）；
    * rejected / failed —— 重置为待处理后按序重派。
    """
    op = IngestOperation.objects.select_related("receipt").get(pk=operation_id)
    receipt = op.receipt
    if receipt.status == IngestReceipt.Status.PROCESSED:
        raise AlreadyProcessed()

    wm = _lock_watermark(op.device_id)
    if receipt.status != IngestReceipt.Status.PENDING:
        receipt.status = IngestReceipt.Status.PENDING
        receipt.status_note = "人工重试排队中"
        receipt.save(update_fields=["status", "status_note", "updated_at"])
        # 终态 -> 待处理会改变连续水位，先重算再进入处理循环
        _refresh_watermarks(wm)
    _process_device_locked(wm)

    receipt.refresh_from_db()
    return receipt
