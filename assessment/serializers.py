"""DRF 序列化器。"""
from django.contrib.gis.geos import Point
from django.db import models
from rest_framework import serializers
from rest_framework_gis.fields import GeometryField
from drf_spectacular.utils import extend_schema_field

from assessment.models import (
    CleaningContract,
    CollectionDevice,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    IngestedOperation,
    OperationReceipt,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ReviewRecord,
    RoadGrid,
    StagedMedia,
)
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.phash import compute_phash_hex


class RoadGridSerializer(serializers.ModelSerializer):
    """道路网格；geom 为 GeoJSON Polygon（EPSG:4326）。"""

    geom = GeometryField()

    class Meta:
        model = RoadGrid
        fields = ["id", "code", "name", "geom"]


class CleaningContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = CleaningContract
        fields = ["id", "code", "grid", "contractor_name", "valid_from", "valid_to", "created_at"]
        read_only_fields = ["created_at"]


class EvidencePhotoSerializer(serializers.ModelSerializer):
    # 上传时只给经纬度，服务端构造 Point；location 以 GeoJSON 只读返回
    lat = serializers.FloatField(write_only=True, min_value=-90, max_value=90)
    lng = serializers.FloatField(write_only=True, min_value=-180, max_value=180)
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = [
            "id", "image", "phash", "captured_at", "lat", "lng",
            "location", "uploader", "note", "event", "created_at",
        ]
        read_only_fields = ["phash", "event", "created_at"]

    def create(self, validated_data):
        lat = validated_data.pop("lat")
        lng = validated_data.pop("lng")
        image = validated_data["image"]
        validated_data["phash"] = compute_phash_hex(image)
        image.seek(0)
        validated_data["location"] = Point(lng, lat, srid=4326)
        photo = EvidencePhoto.objects.create(**validated_data)
        # 仅依据 pHash 生成疑似重复候选；不做任何自动合并
        generate_candidates_for_photo(photo)
        return photo


class PhotoBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = ["id", "phash", "captured_at", "location", "event", "note"]


class DuplicateCandidateSerializer(serializers.ModelSerializer):
    photo = PhotoBriefSerializer(read_only=True)
    matched_photo = PhotoBriefSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DuplicateCandidate
        fields = [
            "id", "photo", "matched_photo", "hamming_distance",
            "status", "status_display", "decision_note", "decided_by", "decided_at",
        ]
        read_only_fields = fields


class CandidateDecisionSerializer(serializers.Serializer):
    class Action(models.TextChoices):
        ATTACH = "attach", "确认同一问题，挂接到被匹配事件（不重复扣分）"
        CREATE_NEW = "create_new", "另立新事件（整改后复发 / 不同地点）"
        DIFFERENT = "different", "标记为不同，暂不处理"
        REJECTED = "rejected", "误报忽略"

    action = serializers.ChoiceField(choices=Action.choices)
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)


class CreateEventFromPhotoSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    occurred_at = serializers.DateTimeField(required=False)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class RectificationReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rectification
        fields = ["id", "event", "photo", "note", "submitted_by", "submitted_at"]


class RectifyRequestSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)
    photo_id = serializers.PrimaryKeyRelatedField(
        queryset=EvidencePhoto.objects.all(), required=False, allow_null=True,
    )
    now = serializers.DateTimeField(required=False, help_text="可选：注入整改提交时间")


class PenaltyVersionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = PenaltyVersion
        fields = [
            "id", "version_no", "points", "escalation_level",
            "kind", "kind_display", "reason", "actor", "created_at",
        ]
        read_only_fields = fields


class EscalationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = EscalationRecord
        fields = ["id", "penalty", "level", "version", "reason", "actor", "ran_at"]
        read_only_fields = fields


class ReviewRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewRecord
        fields = ["id", "penalty", "version", "approved", "comment", "reviewer", "reviewed_at"]
        read_only_fields = fields


class EventBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "category", "status", "location",
            "occurred_at", "grid", "contractor_name",
        ]


class PenaltyUnitSerializer(serializers.ModelSerializer):
    event = EventBriefSerializer(read_only=True)
    versions = PenaltyVersionSerializer(many=True, read_only=True)
    escalations = EscalationRecordSerializer(many=True, read_only=True)
    reviews = ReviewRecordSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    locked_version_no = serializers.SerializerMethodField()

    class Meta:
        model = PenaltyUnit
        fields = [
            "id", "penalty_no", "event", "contract", "contractor_name",
            "points", "escalation_level", "status", "status_display",
            "locked_version", "locked_version_no",
            "versions", "escalations", "reviews", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_locked_version_no(self, obj) -> int | None:
        return obj.locked_version.version_no if obj.locked_version_id else None


class PenaltyCorrectionSerializer(serializers.Serializer):
    points = serializers.DecimalField(max_digits=6, decimal_places=1, min_value=0)
    reason = serializers.CharField(max_length=512)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class PenaltyReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="reviewer", max_length=64)


class EscalationRunSerializer(serializers.Serializer):
    now = serializers.DateTimeField(required=False, help_text="注入的当前时间；不传则用服务器时钟")
    default_sla_hours = serializers.IntegerField(required=False, min_value=1)
    actor = serializers.CharField(required=False, default="escalation-job", max_length=64)


# ---------------------------------------------------------------- 采集包接入


class StagedMediaUploadSerializer(serializers.Serializer):
    """暂存媒体上传（multipart）。同一 (设备, media_key) 重传幂等。"""

    device_no = serializers.CharField(max_length=64)
    media_key = serializers.CharField(max_length=64)
    image = serializers.ImageField()


class StagedMediaReadSerializer(serializers.ModelSerializer):
    device_no = serializers.CharField(source="device.device_no", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = StagedMedia
        fields = [
            "id", "device_no", "media_key", "image", "sha256",
            "status", "status_display", "consumed_by", "created_at",
        ]
        read_only_fields = fields


class PhotoReportPayloadSerializer(serializers.Serializer):
    """问题照片立案参数：照片引用（媒体键）+ 立案资料 + 位置。"""

    media_key = serializers.CharField(max_length=64, help_text="暂存媒体的客户端媒体键")
    category = serializers.ChoiceField(
        choices=ProblemEvent.Category.choices, required=False, default=ProblemEvent.Category.OTHER,
    )
    description = serializers.CharField(required=False, allow_blank=True, default="", max_length=512)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    uploader = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=256)


class RectificationPayloadSerializer(serializers.Serializer):
    """整改回执参数：event_no 或 report_op_id 二之一定位事件，可附整改后照片。"""

    event_no = serializers.CharField(required=False, max_length=32)
    report_op_id = serializers.CharField(required=False, max_length=64)
    media_key = serializers.CharField(required=False, max_length=64)
    lng = serializers.FloatField(required=False, min_value=-180, max_value=180)
    lat = serializers.FloatField(required=False, min_value=-90, max_value=90)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=512)
    actor = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)

    def validate(self, attrs):
        if bool(attrs.get("event_no")) == bool(attrs.get("report_op_id")):
            raise serializers.ValidationError("event_no 与 report_op_id 必须且只能提供一个")
        if ("lng" in attrs) != ("lat" in attrs):
            raise serializers.ValidationError("lng/lat 必须同时提供")
        return attrs


_PAYLOAD_SERIALIZERS = {
    IngestedOperation.OpType.PHOTO_REPORT: PhotoReportPayloadSerializer,
    IngestedOperation.OpType.RECTIFICATION: RectificationPayloadSerializer,
}


class IngestOperationSerializer(serializers.Serializer):
    """单条设备操作：稳定操作号 + 设备内序号 + 业务发生时间 + 业务参数。"""

    op_id = serializers.CharField(max_length=64)
    seq = serializers.IntegerField(min_value=1)
    type = serializers.ChoiceField(choices=IngestedOperation.OpType.choices)
    occurred_at = serializers.DateTimeField(help_text="业务发生时间（归属/整改均以它为准）")
    payload = serializers.JSONField()

    def validate(self, attrs):
        payload_serializer = _PAYLOAD_SERIALIZERS[attrs["type"]](data=attrs["payload"])
        payload_serializer.is_valid(raise_exception=True)
        attrs["payload"] = payload_serializer.validated_data
        return attrs


class IngestBatchSerializer(serializers.Serializer):
    """采集包：同一设备的一批操作。逐条入账，逐条回执。"""

    device_no = serializers.CharField(max_length=64)
    batch_no = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    operations = IngestOperationSerializer(many=True)

    def validate_operations(self, operations):
        if not operations:
            raise serializers.ValidationError("operations 不能为空")
        for field in ("seq", "op_id"):
            values = [op[field] for op in operations]
            if len(values) != len(set(values)):
                raise serializers.ValidationError(f"同一批次内 {field} 重复")
        return operations


class IngestReceiptItemSerializer(serializers.Serializer):
    """批量接入响应中的逐条回执（status=conflict 表示与账本冲突、未入账）。"""

    receipt_id = serializers.IntegerField(allow_null=True)
    op_id = serializers.CharField()
    seq = serializers.IntegerField()
    status = serializers.ChoiceField(
        choices=[*OperationReceipt.Status.values, "conflict"],
    )
    replayed = serializers.BooleanField()
    result = serializers.JSONField(allow_null=True)
    error = serializers.JSONField(allow_null=True)


class IngestBatchResponseSerializer(serializers.Serializer):
    device_no = serializers.CharField()
    received_watermark = serializers.IntegerField()
    applied_watermark = serializers.IntegerField()
    receipts = IngestReceiptItemSerializer(many=True)


class CollectionDeviceSerializer(serializers.ModelSerializer):
    applied_count = serializers.IntegerField(read_only=True)
    pending_count = serializers.IntegerField(read_only=True)
    ignored_count = serializers.IntegerField(read_only=True)
    failed_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CollectionDevice
        fields = [
            "id", "device_no", "name",
            "received_watermark", "applied_watermark", "last_seen_at",
            "applied_count", "pending_count", "ignored_count", "failed_count",
            "created_at",
        ]
        read_only_fields = fields


class OperationReceiptSerializer(serializers.ModelSerializer):
    """逐条回执（含账本原文，支撑全链路追溯）。"""

    device_no = serializers.CharField(source="operation.device.device_no", read_only=True)
    op_id = serializers.CharField(source="operation.op_id", read_only=True)
    seq = serializers.IntegerField(source="operation.seq", read_only=True)
    op_type = serializers.CharField(source="operation.op_type", read_only=True)
    occurred_at = serializers.DateTimeField(source="operation.occurred_at", read_only=True)
    payload = serializers.JSONField(source="operation.payload", read_only=True)
    batch_no = serializers.CharField(source="operation.batch_no", read_only=True)
    received_at = serializers.DateTimeField(source="operation.received_at", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = OperationReceipt
        fields = [
            "id", "device_no", "op_id", "seq", "op_type", "occurred_at",
            "payload", "batch_no", "received_at",
            "status", "status_display", "result", "error", "attempts", "processed_at",
        ]
        read_only_fields = fields


class ProblemEventSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)
    photos = PhotoBriefSerializer(many=True, read_only=True)
    penalty = PenaltyUnitSerializer(read_only=True)
    rectification = RectificationReadSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "primary_photo", "grid", "contract", "contractor_name",
            "category", "category_display", "description", "location",
            "occurred_at", "status", "status_display", "sla_hours", "dedup_key",
            "photos", "penalty", "rectification", "created_at",
        ]
        read_only_fields = fields
