"""DRF 序列化器。"""
from django.contrib.gis.geos import Point
from django.db import models
from rest_framework import serializers
from rest_framework_gis.fields import GeometryField
from drf_spectacular.utils import extend_schema_field

from assessment.models import (
    CleaningContract,
    DeviceWatermark,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    IngestMedia,
    IngestOperation,
    IngestReceipt,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ReviewRecord,
    RoadGrid,
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


# ---------------------------------------------------------------------------
# 离线采集包接入
# ---------------------------------------------------------------------------

def _validate_photo_refs(value, *, required):
    if value is None:
        return "至少一个照片引用（媒体号字符串列表）" if required else None
    if not isinstance(value, list) or (required and not value):
        return "照片引用必须是非空媒体号字符串列表"
    if not all(isinstance(item, str) and item for item in value):
        return "照片引用必须是媒体号字符串列表"
    return None


def _validate_lng_lat(payload, *, required):
    errors = {}
    lng, lat = payload.get("lng"), payload.get("lat")
    for name, value, bound in (("lng", lng, 180), ("lat", lat, 90)):
        if value is None:
            if required:
                errors[name] = "必填"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -bound <= value <= bound:
            errors[name] = f"必须在 [-{bound}, {bound}] 范围内"
    if not required and (lng is None) != (lat is None):
        errors["lng"] = "经纬度必须同时提供"
    return errors


class IngestOperationInputSerializer(serializers.Serializer):
    """
    单条设备操作。payload 按 op_type 约定：
    * report  —— {lng, lat, photo_refs[], category?, description?, note?}
    * rectify —— {event_no | report_operation_no, note?, photo_refs[]?, lng?, lat?}
    """

    operation_no = serializers.CharField(max_length=64, help_text="稳定操作号（设备内唯一，重放凭它幂等）")
    seq = serializers.IntegerField(min_value=1, help_text="设备内序号，从 1 开始连续递增")
    op_type = serializers.ChoiceField(choices=IngestOperation.OpType.choices)
    occurred_at = serializers.DateTimeField(help_text="业务发生时间（归属/整改以它为准）")
    payload = serializers.DictField()

    def validate(self, attrs):
        payload = attrs["payload"]
        errors = {}
        if attrs["op_type"] == IngestOperation.OpType.REPORT:
            errors.update(_validate_lng_lat(payload, required=True))
            refs_error = _validate_photo_refs(payload.get("photo_refs"), required=True)
            if refs_error:
                errors["photo_refs"] = refs_error
            category = payload.get("category")
            if category is not None and category not in ProblemEvent.Category.values:
                errors["category"] = f"未知问题类别 {category}"
        else:  # rectify
            if not payload.get("event_no") and not payload.get("report_operation_no"):
                errors["event_no"] = "整改回执必须引用 event_no 或 report_operation_no 之一"
            refs_error = _validate_photo_refs(payload.get("photo_refs"), required=False)
            if refs_error:
                errors["photo_refs"] = refs_error
            errors.update(_validate_lng_lat(payload, required=False))
        if errors:
            raise serializers.ValidationError({"payload": errors})
        return attrs


class IngestBatchRequestSerializer(serializers.Serializer):
    """批量接入请求：同一设备的一批操作（允许夹杂重放）。"""

    device_id = serializers.CharField(max_length=64)
    operations = IngestOperationInputSerializer(many=True, max_length=200)


class IngestReceiptSerializer(serializers.Serializer):
    """逐条回执。"""

    operation_id = serializers.IntegerField()
    operation_no = serializers.CharField()
    seq = serializers.IntegerField()
    op_type = serializers.CharField()
    status = serializers.ChoiceField(choices=IngestReceipt.Status.choices)
    replayed = serializers.BooleanField(help_text="本次提交是否为重放（未重复执行）")
    result = serializers.JSONField(allow_null=True)
    error_code = serializers.CharField(allow_blank=True)
    error_detail = serializers.CharField(allow_blank=True)
    status_note = serializers.CharField(allow_blank=True)
    attempts = serializers.IntegerField()
    received_at = serializers.DateTimeField()
    processed_at = serializers.DateTimeField(allow_null=True)


class DeviceWatermarkSerializer(serializers.ModelSerializer):
    status_counts = serializers.SerializerMethodField()

    class Meta:
        model = DeviceWatermark
        fields = ["device_id", "received_through", "processed_through", "status_counts", "updated_at"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField)
    def get_status_counts(self, obj) -> dict:
        counts = {key: 0 for key in IngestReceipt.Status.values}
        for row in (
            IngestReceipt.objects.filter(operation__device_id=obj.device_id)
            .values("status")
            .annotate(total=models.Count("id"))
        ):
            counts[row["status"]] = row["total"]
        return counts


class IngestBatchResponseSerializer(serializers.Serializer):
    device_id = serializers.CharField()
    watermark = DeviceWatermarkSerializer()
    receipts = IngestReceiptSerializer(many=True)


class IngestOperationReadSerializer(serializers.ModelSerializer):
    """账本行 + 当前回执（状态查询）。"""

    receipt = serializers.SerializerMethodField()

    class Meta:
        model = IngestOperation
        fields = [
            "id", "device_id", "operation_no", "seq", "op_type", "occurred_at",
            "payload", "payload_hash", "received_at", "receipt",
        ]
        read_only_fields = fields

    @extend_schema_field(IngestReceiptSerializer)
    def get_receipt(self, obj) -> dict:
        from assessment.services.ingest import receipt_to_dict

        return receipt_to_dict(obj)


class IngestMediaSerializer(serializers.ModelSerializer):
    reused = serializers.SerializerMethodField(help_text="本次上传是否为重传（内容一致，未重复落盘）")

    class Meta:
        model = IngestMedia
        fields = ["id", "media_id", "device_id", "image", "phash", "sha256", "photo", "reused", "created_at"]
        read_only_fields = ["phash", "sha256", "photo", "created_at"]
        # 幂等重传由服务层判定（相同内容返回原记录），不能让唯一性校验器提前 400
        extra_kwargs = {"media_id": {"validators": []}}

    @extend_schema_field(serializers.BooleanField)
    def get_reused(self, obj) -> bool:
        return bool(getattr(obj, "_reused", False))
