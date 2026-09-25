"""
采集包接入服务：离线设备的媒体暂存、批量操作接收、按序执行与逐条回执。

核心语义：
* 接收账本不可变 —— 同一 (设备, 操作号) 只入账一次；重放返回原回执（原结果），
  内容指纹不一致或序号复用视为冲突，账本绝不被改写；
* 设备内序号有序 —— 只有前序全部处理完才执行本操作：缺失前序的操作保持
  “待处理”，缺口补齐后按序号顺序级联执行，绝不按到达顺序篡改事件；
* 执行原子化 —— 单条操作的业务写入（证据照片/事件/处罚/候选）在同一事务，
  失败整体回滚并记入回执，可安全重试，不留半个事件、处罚或相似照片候选；
* 业务规则复用 —— 立案归属（发生时合同）、人工去重候选、整改、复核锁定
  全部走既有领域服务，接入层不另开捷径。
"""
import hashlib
import json
import os
import re

from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction
from django.utils import timezone

from assessment.exceptions import (
    AlreadyRectified,
    DomainError,
    InvalidReference,
    InvalidRetry,
    MediaKeyConflict,
    MediaNotAvailable,
)
from assessment.models import (
    CollectionDevice,
    EvidencePhoto,
    IngestedOperation,
    OperationReceipt,
    ProblemEvent,
    Rectification,
    StagedMedia,
)
from assessment.services.clock import FixedClock
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.events import create_event_from_photo
from assessment.services.phash import compute_phash_hex
from assessment.services.rectification import submit_rectification


# ---------------------------------------------------------------- 工具

def _canonical_hash(data: dict) -> str:
    """操作内容的稳定指纹（重放一致性校验用）。"""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file_sha256(image_file) -> str:
    image_file.seek(0)
    digest = hashlib.sha256()
    for chunk in image_file.chunks():
        digest.update(chunk)
    image_file.seek(0)
    return digest.hexdigest()


def _error_code(exc: DomainError) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()


def get_or_create_device(device_no: str) -> CollectionDevice:
    device, _ = CollectionDevice.objects.get_or_create(device_no=device_no)
    return device


# ---------------------------------------------------------------- 媒体暂存

@transaction.atomic
def stage_media(*, device_no: str, media_key: str, image_file) -> tuple[StagedMedia, bool]:
    """
    暂存一份媒体文件，返回 (media, created)。
    按 (设备, media_key) 幂等：重传同键同内容返回原记录（上传中断可安全重传）；
    同键不同内容 -> 409。并发同键插入时清理孤儿文件后改读已有行。
    """
    device = get_or_create_device(device_no)
    digest = _file_sha256(image_file)

    existing = StagedMedia.objects.filter(device=device, media_key=media_key).first()
    if existing is not None:
        if existing.sha256 != digest:
            raise MediaKeyConflict(f"媒体键 {media_key} 已存在但内容不一致")
        return existing, False

    media = StagedMedia(device=device, media_key=media_key, sha256=digest)
    media.image.save(os.path.basename(image_file.name), image_file, save=False)
    try:
        with transaction.atomic():  # 保存点：并发同键时回滚本次插入
            media.save()
    except IntegrityError:
        media.image.delete(save=False)  # 清理孤儿文件，不留半截上传
        existing = StagedMedia.objects.get(device=device, media_key=media_key)
        if existing.sha256 != digest:
            raise MediaKeyConflict(f"媒体键 {media_key} 已存在但内容不一致")
        return existing, False
    return media, True


# ---------------------------------------------------------------- 批量接入

def ingest_batch(*, device_no: str, operations: list[dict], batch_no: str = "") -> dict:
    """
    接收一批设备操作：逐条独立事务入账并尝试级联执行。
    返回设备水位与逐条回执（含重放/冲突条目）。
    """
    device = get_or_create_device(device_no)
    receipts = [
        receive_operation(device, op_data, batch_no=batch_no)
        for op_data in sorted(operations, key=lambda op: op["seq"])
    ]
    device.refresh_from_db()
    return {
        "device_no": device.device_no,
        "received_watermark": device.received_watermark,
        "applied_watermark": device.applied_watermark,
        "receipts": receipts,
    }


def receive_operation(device: CollectionDevice, op_data: dict, *, batch_no: str = "") -> dict:
    """接收单条操作（独立事务）：查重 -> 入账 -> 推进水位 -> 级联执行。"""
    op_id, seq = op_data["op_id"], op_data["seq"]
    fingerprint = _canonical_hash({
        "op_id": op_id,
        "seq": seq,
        "type": op_data["type"],
        "occurred_at": op_data["occurred_at"].isoformat(),
        "payload": op_data["payload"],
    })
    with transaction.atomic():
        # 锁设备行：同一设备的接收与执行串行化
        device = CollectionDevice.objects.select_for_update().get(pk=device.pk)
        device.last_seen_at = timezone.now()

        existing = IngestedOperation.objects.filter(device=device, op_id=op_id).first()
        if existing is not None:
            device.save(update_fields=["last_seen_at", "updated_at"])
            if existing.payload_hash != fingerprint:
                # 同一操作号携带不同内容：账本不可变，拒绝覆盖
                return _conflict_item(op_id, seq, "op_id_redefined",
                                      f"操作号 {op_id} 已接收过且内容不一致")
            return _receipt_item(existing.receipt, replayed=True)  # 重放返回原结果

        if IngestedOperation.objects.filter(device=device, seq=seq).exists():
            device.save(update_fields=["last_seen_at", "updated_at"])
            return _conflict_item(op_id, seq, "seq_reused", f"序号 {seq} 已被其他操作占用")

        operation = IngestedOperation.objects.create(
            device=device,
            op_id=op_id,
            seq=seq,
            op_type=op_data["type"],
            occurred_at=op_data["occurred_at"],
            payload=op_data["payload"],
            payload_hash=fingerprint,
            batch_no=batch_no,
        )
        OperationReceipt.objects.create(operation=operation)
        _advance_received_watermark(device)
        _cascade_apply(device)
        device.save(update_fields=["received_watermark", "applied_watermark", "last_seen_at", "updated_at"])
        return _receipt_item(OperationReceipt.objects.get(operation=operation), replayed=False)


def retry_operation(receipt: OperationReceipt) -> OperationReceipt:
    """
    重试失败的操作：复位为待处理后从设备水位处重新级联执行。
    仅 failed 状态可重试；pending（前序未齐）/ applied / ignored 一律 409。
    """
    with transaction.atomic():
        receipt = OperationReceipt.objects.select_for_update().get(pk=receipt.pk)
        if receipt.status != OperationReceipt.Status.FAILED:
            raise InvalidRetry(
                f"回执状态为 {receipt.get_status_display()}，仅失败状态可重试"
            )
        device = CollectionDevice.objects.select_for_update().get(pk=receipt.operation.device_id)
        receipt.status = OperationReceipt.Status.PENDING
        receipt.error = {}
        receipt.save(update_fields=["status", "error", "updated_at"])
        _cascade_apply(device)
        device.save(update_fields=["received_watermark", "applied_watermark", "updated_at"])
        receipt.refresh_from_db()
        return receipt


# ---------------------------------------------------------------- 水位与级联

def _advance_received_watermark(device: CollectionDevice) -> None:
    seq = device.received_watermark
    while IngestedOperation.objects.filter(device=device, seq=seq + 1).exists():
        seq += 1
    device.received_watermark = seq


def _cascade_apply(device: CollectionDevice) -> None:
    """
    从设备处理水位之后第一条开始，按序号连续执行已接收的操作。
    缺口（前序未接收）或执行失败都会停下——补齐/重试成功后再从这里继续。
    """
    seq = device.applied_watermark + 1
    while True:
        operation = (
            IngestedOperation.objects
            .filter(device=device, seq=seq)
            .select_related("receipt")
            .first()
        )
        if operation is None:
            break  # 前序缺失：后续操作保持待处理
        receipt = operation.receipt
        if receipt.status != OperationReceipt.Status.PENDING:
            break  # 已处理过（失败的头等待重试）
        _apply_once(operation, receipt)
        if receipt.status == OperationReceipt.Status.FAILED:
            break  # 失败阻塞链条，等待重试
        device.applied_watermark = seq
        seq += 1


def _apply_once(operation: IngestedOperation, receipt: OperationReceipt) -> None:
    """执行一条操作：业务写入全部成功才算成功，任何失败整体回滚。"""
    receipt.attempts += 1
    try:
        with transaction.atomic():  # 内层保存点：业务写入失败回滚，回执照常落库
            if operation.op_type == IngestedOperation.OpType.PHOTO_REPORT:
                outcome = _apply_photo_report(operation)
            elif operation.op_type == IngestedOperation.OpType.RECTIFICATION:
                outcome = _apply_rectification(operation)
            else:
                raise InvalidReference(f"未知操作类型 {operation.op_type}")
        receipt.status, receipt.result = outcome
        receipt.error = {}
    except AlreadyRectified as exc:
        # 并发下被其他渠道整改：业务幂等拒绝视为已处理，不阻塞后续序号
        receipt.status = OperationReceipt.Status.IGNORED
        receipt.result = {"detail": str(exc.detail)}
        receipt.error = {}
    except DomainError as exc:
        receipt.status = OperationReceipt.Status.FAILED
        receipt.error = {
            "code": _error_code(exc),
            "detail": exc.detail,
            "http_status": exc.status_code,
        }
    receipt.processed_at = timezone.now()
    receipt.save()


# ---------------------------------------------------------------- 各操作类型

def _apply_photo_report(operation: IngestedOperation) -> tuple[str, dict]:
    """问题照片立案：暂存媒体 -> 证据照片 -> 事件/处罚（发生时归属）-> 人工去重候选。"""
    payload = operation.payload
    photo = _consume_media(
        operation,
        payload["media_key"],
        location=Point(payload["lng"], payload["lat"], srid=4326),
    )
    event = create_event_from_photo(
        photo,
        category=payload.get("category") or ProblemEvent.Category.OTHER,
        description=payload.get("description", ""),
        actor=payload.get("uploader") or operation.device.device_no,
        occurred_at=operation.occurred_at,  # 归属按业务发生时间，与接收时间无关
    )
    # 与直传一致：生成疑似重复候选，交人工判定（不自动合并）
    candidates = generate_candidates_for_photo(photo)
    return OperationReceipt.Status.APPLIED, {
        "photo_id": photo.id,
        "event_id": event.id,
        "event_no": event.event_no,
        "penalty_no": event.penalty.penalty_no,
        "contractor_name": event.contractor_name,
        "candidate_ids": [candidate.id for candidate in candidates],
    }


def _apply_rectification(operation: IngestedOperation) -> tuple[str, dict]:
    """整改回执：定位事件 -> （可选）整改照片 -> 提交整改（提交时间=业务发生时间）。"""
    payload = operation.payload
    event = _resolve_event(operation)

    existing = Rectification.objects.filter(event=event).first()
    if existing is not None:
        # 迟到的重复整改：不反转已结案事件，回执标记为已忽略（视为已处理）
        return OperationReceipt.Status.IGNORED, {
            "event_no": event.event_no,
            "rectification_id": existing.id,
            "detail": "事件已整改，迟到回执已忽略",
        }

    photo = None
    candidate_ids = []
    if payload.get("media_key"):
        lng, lat = payload.get("lng"), payload.get("lat")
        location = (
            Point(lng, lat, srid=4326)
            if lng is not None and lat is not None
            else event.location
        )
        photo = _consume_media(operation, payload["media_key"], location=location, event=event)
        # 与直传一致：整改照片同样进入人工去重候选
        candidate_ids = [c.id for c in generate_candidates_for_photo(photo)]

    rectification = submit_rectification(
        event,
        note=payload.get("note", ""),
        actor=payload.get("actor") or operation.device.device_no,
        photo=photo,
        clock=FixedClock(operation.occurred_at),
    )
    result = {"event_no": event.event_no, "rectification_id": rectification.id}
    if photo is not None:
        result["photo_id"] = photo.id
        result["candidate_ids"] = candidate_ids
    return OperationReceipt.Status.APPLIED, result


def _resolve_event(operation: IngestedOperation) -> ProblemEvent:
    """整改目标：event_no 直接定位，或 report_op_id 引用本设备此前的立案操作。"""
    payload = operation.payload
    event_no = payload.get("event_no")
    if event_no:
        event = ProblemEvent.objects.filter(event_no=event_no).first()
        if event is None:
            raise InvalidReference(f"事件 {event_no} 不存在")
        return event

    report_op_id = payload.get("report_op_id")
    report = (
        IngestedOperation.objects
        .filter(device=operation.device, op_id=report_op_id)
        .select_related("receipt")
        .first()
    )
    if report is None:
        raise InvalidReference(f"本设备不存在立案操作 {report_op_id}")
    if report.receipt.status != OperationReceipt.Status.APPLIED:
        raise InvalidReference(f"立案操作 {report_op_id} 尚未成功执行")
    event = ProblemEvent.objects.filter(pk=report.receipt.result.get("event_id")).first()
    if event is None:
        raise InvalidReference(f"立案操作 {report_op_id} 未产生事件")
    return event


def _consume_media(
    operation: IngestedOperation,
    media_key: str,
    *,
    location,
    event: ProblemEvent | None = None,
) -> EvidencePhoto:
    """
    把暂存媒体转为证据照片（与所在操作同事务）：
    失败则媒体不被消费、不产生证据照片，重试可安全重来。
    """
    media = (
        StagedMedia.objects
        .select_for_update()
        .filter(device=operation.device, media_key=media_key)
        .first()
    )
    if media is None:
        raise MediaNotAvailable(f"媒体 {media_key} 尚未上传，请先上传媒体再重试")
    if media.status == StagedMedia.Status.CONSUMED:
        raise MediaNotAvailable(f"媒体 {media_key} 已被其他操作消费")

    photo = EvidencePhoto(
        captured_at=operation.occurred_at,
        location=location,
        uploader=operation.payload.get("uploader", ""),
        note=operation.payload.get("note", ""),
        event=event,
    )
    with media.image.open("rb") as src:
        photo.phash = compute_phash_hex(src)
        src.seek(0)
        photo.image.save(os.path.basename(media.image.name), src, save=False)
    photo.save()

    media.status = StagedMedia.Status.CONSUMED
    media.consumed_by = photo
    media.save(update_fields=["status", "consumed_by", "updated_at"])
    return photo


# ---------------------------------------------------------------- 响应条目

def _receipt_item(receipt: OperationReceipt, *, replayed: bool) -> dict:
    return {
        "receipt_id": receipt.id,
        "op_id": receipt.operation.op_id,
        "seq": receipt.operation.seq,
        "status": receipt.status,
        "replayed": replayed,
        "result": receipt.result or None,
        "error": receipt.error or None,
    }


def _conflict_item(op_id: str, seq: int, code: str, detail: str) -> dict:
    return {
        "receipt_id": None,
        "op_id": op_id,
        "seq": seq,
        "status": "conflict",
        "replayed": False,
        "result": None,
        "error": {"code": code, "detail": detail},
    }
