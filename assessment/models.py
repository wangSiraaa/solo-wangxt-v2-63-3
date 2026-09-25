"""
考核领域模型：

道路网格 RoadGrid ──┐
                  ├──< CleaningContract（合同责任区间：网格 × 起止时间 × 承包商）
                  └──< ProblemEvent（问题事件：发生位置、发生时间）
                              │
                   EvidencePhoto（感知哈希 + 候选关联，可挂接到事件）
                              │
                     PenaltyUnit 1:1（唯一扣分单元，归属发生时的合同）
                              │
              PenaltyVersion（只追加、不可变）/ ReviewRecord / EscalationRecord

离线采集包接入：
  CollectionDevice（设备 + 接收/处理水位）
      ├──< StagedMedia（暂存媒体，按 media_key 幂等，消费后转为证据照片）
      └──< IngestedOperation（不可变接收账本：op_id/seq 双唯一）
                  └── OperationReceipt 1:1（逐条回执：状态 + 结果快照）
"""
import uuid

from django.contrib.gis.db import models as gis
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class RoadGrid(gis.Model):
    """道路网格（PostGIS 多边形，SRID 4326）。"""

    code = models.CharField("网格编号", max_length=32, unique=True)
    name = models.CharField("网格名称", max_length=128, blank=True)
    geom = gis.PolygonField("网格范围", srid=4326)

    class Meta:
        verbose_name = "道路网格"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}".strip()


class CleaningContract(TimeStamped):
    """
    保洁合同责任区间：同一网格在同一时间只允许一个生效合同
    （业务上由归属服务校验重叠，录入端也应避免重叠）。
    扣分归属按“事件发生时刻落在哪个 [valid_from, valid_to) 区间”确定。
    """

    code = models.CharField("合同编号", max_length=32, unique=True)
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="contracts", verbose_name="道路网格")
    contractor_name = models.CharField("承包商名称", max_length=128)
    valid_from = models.DateTimeField("责任开始时间（含）")
    valid_to = models.DateTimeField("责任结束时间（不含）")

    class Meta:
        verbose_name = "保洁合同"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["grid", "valid_from", "valid_to"]),
            models.Index(fields=["contractor_name"]),
        ]

    def __str__(self):
        return f"{self.code} {self.contractor_name}"


class ProblemEvent(TimeStamped):
    """
    现场问题事件。一个事件 = 一次扣分口径；不同角度照片挂同一事件，
    整改后复发必须新建事件。
    """

    class Category(models.TextChoices):
        LITTER = "litter", "散落垃圾"
        OVERFLOW = "overflow", "垃圾桶满溢"
        ROAD_DIRT = "road_dirt", "路面污渍"
        DEAD_CORNER = "dead_corner", "卫生死角"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        OPEN = "open", "待整改"
        RECTIFIED = "rectified", "已整改"

    event_no = models.CharField("事件编号", max_length=32, unique=True, editable=False)
    primary_photo = gis.ForeignKey(
        "EvidencePhoto",
        on_delete=models.PROTECT,
        related_name="primary_of",
        null=True,
        blank=True,
        verbose_name="首报照片",
    )
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="events", null=True, verbose_name="所在网格")
    # 归属快照：按“发生时刻”解析出的合同；后续合同/承包商变更不改变历史归属
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="events",
        null=True, verbose_name="责任合同（按发生时解析）",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    category = models.CharField("问题类别", max_length=20, choices=Category.choices)
    description = models.CharField("问题描述", max_length=512, blank=True)
    location = gis.PointField("发生位置", srid=4326)
    occurred_at = models.DateTimeField("发生/拍摄时间", help_text="归属与 SLA 都以该时间为准，而不是录入时间")
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.OPEN)
    sla_hours = models.PositiveIntegerField("整改时限(小时)", default=24)
    # 外部业务幂等键，防止同一条立案请求重复提交
    dedup_key = models.CharField("外部去重键", max_length=64, null=True, blank=True, unique=True)

    class Meta:
        verbose_name = "问题事件"
        verbose_name_plural = verbose_name
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["status", "occurred_at"]),
            models.Index(fields=["contractor_name"]),
        ]

    def save(self, *args, **kwargs):
        if not self.event_no:
            self.event_no = _new_id("EV")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.event_no


class EvidencePhoto(TimeStamped):
    """
    证据照片。上传时由 Pillow 计算 256 位感知哈希(pHash)。
    pHash 只用于生成 DuplicateCandidate（疑似重复候选）；
    照片是否属于同一事件，最终由人工按位置/时间判断。
    照片一旦创建不可删除、不可篡改（证据保全）。
    """

    image = models.ImageField("图片文件", upload_to="evidence/%Y/%m/%d")
    phash = models.CharField("感知哈希(hex)", max_length=64, db_index=True, editable=False)
    captured_at = models.DateTimeField("拍摄时间")
    location = gis.PointField("拍摄位置", srid=4326)
    uploader = models.CharField("上传人", max_length=64, blank=True, default="")
    note = models.CharField("备注", max_length=256, blank=True)
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.PROTECT, related_name="photos",
        null=True, blank=True, verbose_name="关联事件",
    )

    class Meta:
        verbose_name = "证据照片"
        verbose_name_plural = verbose_name
        ordering = ["-captured_at"]


class DuplicateCandidate(models.Model):
    """
    疑似重复候选（pHash 汉明距离 <= 阈值时生成）。
    只表达“图片相似”，不自动合并事件；最终状态由人工判定给出。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待判定"
        CONFIRMED_DUPLICATE = "confirmed_duplicate", "确认同问题（挂接，不重复扣分）"
        RECURRENCE = "recurrence", "整改后复发（新建事件）"
        DIFFERENT = "different", "不同地点/不同问题（不合并）"
        REJECTED = "rejected", "误报忽略"

    photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="candidates", verbose_name="新照片")
    matched_photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="matched_as", verbose_name="相似照片")
    hamming_distance = models.PositiveSmallIntegerField("pHash汉明距离")
    status = models.CharField("判定状态", max_length=24, choices=Status.choices, default=Status.PENDING)
    decision_note = models.CharField("判定说明", max_length=256, blank=True)
    decided_by = models.CharField("判定人", max_length=64, blank=True)
    decided_at = models.DateTimeField("判定时间", null=True, blank=True)

    class Meta:
        verbose_name = "疑似重复候选"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["photo", "matched_photo"], name="uniq_candidate_pair"),
        ]
        indexes = [models.Index(fields=["status"])]
        ordering = ["-id"]

    def __str__(self):
        return f"{self.photo_id}~{self.matched_photo_id} d={self.hamming_distance} {self.status}"


class Rectification(models.Model):
    """整改记录（每个事件至多一条；重复回调返回 409 并被忽略）。"""

    event = models.OneToOneField(
        ProblemEvent, on_delete=models.PROTECT, related_name="rectification", verbose_name="问题事件",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, related_name="rectifications",
        null=True, blank=True, verbose_name="整改后照片",
    )
    note = models.CharField("整改说明", max_length=512, blank=True)
    submitted_by = models.CharField("提交人", max_length=64, blank=True, default="")
    submitted_at = models.DateTimeField("整改提交时间")

    class Meta:
        verbose_name = "整改记录"
        verbose_name_plural = verbose_name


class PenaltyUnit(TimeStamped):
    """
    处罚单元（扣分的最小且唯一归属单元）：一个事件一个 PenaltyUnit。
    每笔扣分都能通过 penalty_no 追到：事件、责任合同/承包商、全部证据、全部版本。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "待复核"
        LOCKED = "locked", "已锁定"

    penalty_no = models.CharField("处罚单号", max_length=32, unique=True, editable=False)
    event = models.OneToOneField(ProblemEvent, on_delete=models.PROTECT, related_name="penalty", verbose_name="问题事件")
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="penalties",
        null=True, verbose_name="责任合同快照",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    points = models.DecimalField("当前有效扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期升级等级", default=0)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.DRAFT)
    locked_version = models.ForeignKey(
        "PenaltyVersion", on_delete=models.PROTECT, related_name="locked_by_penalties",
        null=True, blank=True, verbose_name="最近锁定版本",
    )

    class Meta:
        verbose_name = "处罚单元"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.penalty_no:
            self.penalty_no = _new_id("PN")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.penalty_no


class PenaltyVersion(models.Model):
    """
    处罚版本——只追加(append-only)、不可变：
    初版立案、逾期升级、人工更正都只能新增一行；
    复核通过时由 PenaltyUnit.locked_version 指向锁定行，历史行永不修改。
    """

    class Kind(models.TextChoices):
        INITIAL = "initial", "初版立案"
        ESCALATION = "escalation", "逾期升级"
        CORRECTION = "correction", "人工更正"

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="versions", verbose_name="处罚单元")
    version_no = models.PositiveIntegerField("版本号")
    points = models.DecimalField("扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期等级", default=0)
    kind = models.CharField("版本类型", max_length=16, choices=Kind.choices)
    reason = models.CharField("原因", max_length=512)
    actor = models.CharField("操作人/任务", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "处罚版本"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "version_no"], name="uniq_penalty_version_no"),
        ]
        ordering = ["penalty_id", "version_no"]


class EscalationRecord(models.Model):
    """逾期升级执行记录（可注入时钟，按 (处罚单, 等级) 幂等）。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="escalations", verbose_name="处罚单元")
    level = models.PositiveSmallIntegerField("升级到等级")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="escalations", verbose_name="产生的处罚版本")
    reason = models.CharField("升级原因", max_length=512)
    actor = models.CharField("执行任务", max_length=64, blank=True, default="")
    ran_at = models.DateTimeField("执行时间(注入时钟)")

    class Meta:
        verbose_name = "升级记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "level"], name="uniq_escalation_level"),
        ]
        ordering = ["penalty_id", "level"]


class CollectionDevice(TimeStamped):
    """
    离线采集设备。服务端按设备维护两个水位（设备内序号从 1 起连续递增）：
    * received_watermark —— 已连续接收到的最大序号；
    * applied_watermark  —— 已连续处理（执行或忽略）到的最大序号。
    缺口之后的操作只入账不执行，补齐后按序号顺序执行。
    """

    device_no = models.CharField("设备编号", max_length=64, unique=True)
    name = models.CharField("设备名称", max_length=128, blank=True)
    received_watermark = models.PositiveIntegerField("已连续接收序号", default=0)
    applied_watermark = models.PositiveIntegerField("已连续处理序号", default=0)
    last_seen_at = models.DateTimeField("最近通信时间", null=True, blank=True)

    class Meta:
        verbose_name = "采集设备"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.device_no


class StagedMedia(TimeStamped):
    """
    暂存媒体：设备先上传照片文件，再在操作中按 media_key 引用。
    按 (设备, media_key) 幂等——上传中断后用同一 media_key 重传返回原记录；
    同键不同内容视为冲突（409）。只有被操作成功消费才转为 EvidencePhoto，
    暂存区本身不产生证据、候选或任何业务结果，因此可安全重试。
    """

    class Status(models.TextChoices):
        STAGED = "staged", "已暂存（待引用）"
        CONSUMED = "consumed", "已消费（已转为证据照片）"

    device = models.ForeignKey(
        CollectionDevice, on_delete=models.PROTECT, related_name="staged_media", verbose_name="采集设备",
    )
    media_key = models.CharField("客户端媒体键", max_length=64)
    image = models.ImageField("图片文件", upload_to="staged/%Y/%m/%d")
    sha256 = models.CharField("内容SHA256", max_length=64, editable=False)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.STAGED)
    consumed_by = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, related_name="staged_sources",
        null=True, blank=True, verbose_name="生成的证据照片",
    )

    class Meta:
        verbose_name = "暂存媒体"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["device", "media_key"], name="uniq_staged_media_key"),
        ]

    def __str__(self):
        return f"{self.device_id}:{self.media_key}"


class IngestedOperation(models.Model):
    """
    接收账本（不可变，append-only）：每条设备操作只插入一次，之后永不修改。
    * (设备, op_id) 唯一 —— 同一操作重放返回原回执；内容指纹不一致视为冲突；
    * (设备, seq)   唯一 —— 设备内序号不可复用；
    处理状态与结果快照见 OperationReceipt（账本与回执分离，账本保持不可变）。
    """

    class OpType(models.TextChoices):
        PHOTO_REPORT = "photo_report", "问题照片立案"
        RECTIFICATION = "rectification", "整改回执"

    device = models.ForeignKey(
        CollectionDevice, on_delete=models.PROTECT, related_name="operations", verbose_name="采集设备",
    )
    op_id = models.CharField("稳定操作号", max_length=64)
    seq = models.PositiveIntegerField("设备内序号")
    op_type = models.CharField("操作类型", max_length=24, choices=OpType.choices)
    occurred_at = models.DateTimeField("业务发生时间", help_text="归属/整改时间以该时间为准，而不是接收时间")
    payload = models.JSONField("业务参数（含照片引用）", default=dict)
    payload_hash = models.CharField("内容指纹(SHA256)", max_length=64, editable=False)
    batch_no = models.CharField("客户端批次号", max_length=64, blank=True, default="")
    received_at = models.DateTimeField("接收时间", auto_now_add=True)

    class Meta:
        verbose_name = "接收账本"
        verbose_name_plural = verbose_name
        ordering = ["device_id", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["device", "op_id"], name="uniq_device_op_id"),
            models.UniqueConstraint(fields=["device", "seq"], name="uniq_device_seq"),
        ]

    def __str__(self):
        return f"{self.device_id}:{self.seq} {self.op_type}"


class OperationReceipt(TimeStamped):
    """
    逐条回执：跟踪账本中每条操作的处理状态与业务结果快照。
    重放同一操作返回该快照（原结果），不重新执行；
    failed 可经重试 API 重新执行，成功后级联处理后续待处理序号。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待处理（前序未齐）"
        APPLIED = "applied", "已执行"
        IGNORED = "ignored", "已忽略（业务幂等拒绝，如迟到的重复整改）"
        FAILED = "failed", "执行失败（可重试）"

    operation = models.OneToOneField(
        IngestedOperation, on_delete=models.PROTECT, related_name="receipt", verbose_name="接收账本",
    )
    status = models.CharField("处理状态", max_length=16, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField("业务结果快照", default=dict, blank=True)
    error = models.JSONField("失败信息", default=dict, blank=True)
    attempts = models.PositiveIntegerField("执行次数", default=0)
    processed_at = models.DateTimeField("处理时间", null=True, blank=True)

    class Meta:
        verbose_name = "操作回执"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.operation_id} {self.status}"


class ReviewRecord(models.Model):
    """复核记录：复核通过即锁定当前版本。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="reviews", verbose_name="处罚单元")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="reviews", verbose_name="被复核版本")
    approved = models.BooleanField("是否通过")
    comment = models.CharField("复核意见", max_length=512, blank=True)
    reviewer = models.CharField("复核人", max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField("复核时间", auto_now_add=True)

    class Meta:
        verbose_name = "复核记录"
        verbose_name_plural = verbose_name
        ordering = ["-reviewed_at"]
