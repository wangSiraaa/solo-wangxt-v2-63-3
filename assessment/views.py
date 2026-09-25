"""API 视图。所有写操作都委托给 services 层（领域规则集中、时钟可注入）。"""
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from assessment.models import (
    CleaningContract,
    DeviceWatermark,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    IngestMedia,
    IngestOperation,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
)
from assessment.serializers import (
    CandidateDecisionSerializer,
    CleaningContractSerializer,
    CreateEventFromPhotoSerializer,
    DeviceWatermarkSerializer,
    DuplicateCandidateSerializer,
    EscalationRecordSerializer,
    EscalationRunSerializer,
    EvidencePhotoSerializer,
    IngestBatchRequestSerializer,
    IngestBatchResponseSerializer,
    IngestMediaSerializer,
    IngestOperationReadSerializer,
    IngestReceiptSerializer,
    PenaltyCorrectionSerializer,
    PenaltyReviewSerializer,
    PenaltyUnitSerializer,
    PenaltyVersionSerializer,
    ProblemEventSerializer,
    RectificationReadSerializer,
    RectifyRequestSerializer,
    RoadGridSerializer,
)
from assessment.services.clock import resolve_clock
from assessment.services.decisions import decide_candidate
from assessment.services.escalation import run_escalation
from assessment.services.events import create_event_from_photo
from assessment.services.ingest import ingest_batch, receipt_to_dict, retry_operation, store_media
from assessment.services.penalties import correct_penalty, review_penalty
from assessment.services.rectification import submit_rectification


class RoadGridViewSet(viewsets.ModelViewSet):
    """道路网格（GeoJSON Feature 输入/输出）。"""

    queryset = RoadGrid.objects.all()
    serializer_class = RoadGridSerializer


class CleaningContractViewSet(viewsets.ModelViewSet):
    """保洁合同责任区间。"""

    queryset = CleaningContract.objects.select_related("grid").all()
    serializer_class = CleaningContractSerializer
    filterset_fields = ["grid", "contractor_name"]


class EvidencePhotoViewSet(viewsets.mixins.CreateModelMixin,
                          viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    证据照片：仅允许上传 / 查询，不允许修改删除（证据保全）。
    上传成功后响应中可查看自动生成的 pHash 与疑似候选（候选在 /candidates/）。
    """

    queryset = EvidencePhoto.objects.all()
    serializer_class = EvidencePhotoSerializer

    @action(detail=True, methods=["post"])
    def create_event(self, request, pk=None):
        """对一张尚未立案的照片直接创建事件（无候选时的入口）。"""
        photo = self.get_object()
        payload = CreateEventFromPhotoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        event = create_event_from_photo(
            photo,
            category=data.get("category", ProblemEvent.Category.OTHER),
            description=data.get("description", ""),
            actor=data.get("actor", "system"),
            dedup_key=data.get("dedup_key"),
            occurred_at=data.get("occurred_at"),
        )
        return Response(ProblemEventSerializer(event).data, status=status.HTTP_201_CREATED)


class DuplicateCandidateViewSet(viewsets.mixins.RetrieveModelMixin,
                                viewsets.mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    """疑似重复候选：只读列表 + 人工判定动作。"""

    queryset = DuplicateCandidate.objects.select_related("photo", "matched_photo").all()
    serializer_class = DuplicateCandidateSerializer
    filterset_fields = ["status", "photo", "matched_photo"]

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """
        人工判定：
        attach=同一问题不同角度（挂接、不重复扣分）；
        create_new=另立新事件（已整改事件复发/不同地点）；
        different=标记不同不合并。
        """
        candidate = self.get_object()
        payload = CandidateDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        decided = decide_candidate(
            candidate,
            action=data["action"],
            actor=data.get("actor", "system"),
            category=data.get("category"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key"),
            note=data.get("note", ""),
        )
        return Response(DuplicateCandidateSerializer(decided).data)


class ProblemEventViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """问题事件：只读 + 整改回调。"""

    queryset = ProblemEvent.objects.select_related(
        "grid", "contract", "penalty", "rectification", "primary_photo",
    ).prefetch_related("photos").all()
    serializer_class = ProblemEventSerializer
    filterset_fields = ["status", "category", "grid", "contract", "contractor_name"]

    @action(detail=True, methods=["post"])
    def rectify(self, request, pk=None):
        """
        整改回调。重复回调返回 409（幂等忽略，不改变扣分）。
        可传 now 注入整改时间（测试/补录场景）。
        """
        event = self.get_object()
        payload = RectifyRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        clock = resolve_clock(data.get("now"))
        rectification = submit_rectification(
            event,
            note=data.get("note", ""),
            actor=data.get("actor", "system"),
            photo=data.get("photo_id"),
            clock=clock,
        )
        event.refresh_from_db()
        return Response(
            {
                "rectification": RectificationReadSerializer(rectification).data,
                "event": ProblemEventSerializer(event).data,
            },
            status=status.HTTP_201_CREATED,
        )


class RectificationViewSet(viewsets.mixins.RetrieveModelMixin,
                           viewsets.mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    queryset = Rectification.objects.select_related("event", "photo").all()
    serializer_class = RectificationReadSerializer
    filterset_fields = ["event"]


class PenaltyUnitViewSet(viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """
    处罚单元：只读检索（含完整版本链/证据/升级/复核记录，支撑扣分追溯），
    并提供“人工更正（追加版本）”与“复核（锁定版本）”两个动作。
    """

    queryset = PenaltyUnit.objects.select_related(
        "event", "contract", "locked_version",
    ).prefetch_related("versions", "escalations", "reviews").all()
    serializer_class = PenaltyUnitSerializer
    filterset_fields = ["status", "contractor_name", "contract", "event"]

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """人工更正：追加一个 correction 版本，历史版本不动。"""
        penalty = self.get_object()
        payload = PenaltyCorrectionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        version = correct_penalty(
            penalty,
            points=data["points"],
            reason=data["reason"],
            actor=data.get("actor", "system"),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data, status=status.HTTP_201_CREATED,
                        headers={"Version-No": str(version.version_no)})

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """复核：approved=true 时锁定当前最新版本。"""
        penalty = self.get_object()
        payload = PenaltyReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        review_penalty(
            penalty,
            approved=data["approved"],
            actor=data.get("actor", "reviewer"),
            comment=data.get("comment", ""),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data)


class PenaltyVersionViewSet(viewsets.mixins.RetrieveModelMixin,
                            viewsets.mixins.ListModelMixin,
                            viewsets.GenericViewSet):
    """处罚版本（只读——版本只追加、不可改）。写方法一律 405。"""

    queryset = PenaltyVersion.objects.all()
    serializer_class = PenaltyVersionSerializer
    filterset_fields = ["penalty", "kind"]


class EscalationRecordViewSet(viewsets.mixins.RetrieveModelMixin,
                              viewsets.mixins.ListModelMixin,
                              viewsets.GenericViewSet):
    queryset = EscalationRecord.objects.select_related("penalty", "version").all()
    serializer_class = EscalationRecordSerializer
    filterset_fields = ["penalty", "level"]

    @action(detail=False, methods=["post"])
    def run(self, request):
        """
        执行一次逾期升级扫描。
        body 可传 {"now": "2026-09-25T10:00:00+08:00"} 注入时钟，
        不传则使用服务器当前时间。重复执行幂等。
        """
        payload = EscalationRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = run_escalation(
            clock=resolve_clock(data.get("now")),
            default_sla_hours=data.get("default_sla_hours"),
            actor=data.get("actor", "escalation-job"),
        )
        return Response(
            {
                "inspected_open_events": result.inspected,
                "overdue_open_events": result.open_overdue,
                "created_count": result.created_count,
                "created": EscalationRecordSerializer(result.created, many=True).data,
            },
            status=status.HTTP_201_CREATED if result.created_count else status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# 离线采集包接入（纯 API，无界面）
# ---------------------------------------------------------------------------


class IngestBatchViewSet(viewsets.GenericViewSet):
    """
    采集包批量接入：设备离线操作按 (操作号, 设备内序号, 业务发生时间, 照片引用) 落账。
    同一操作重放返回原回执；缺失前序的操作保持待处理，补齐后按序号顺序执行。
    """

    serializer_class = IngestBatchRequestSerializer

    @extend_schema(request=IngestBatchRequestSerializer, responses=IngestBatchResponseSerializer)
    def create(self, request):
        payload = IngestBatchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = ingest_batch(device_id=data["device_id"], operations=data["operations"])
        body = {
            "device_id": result.device_id,
            "watermark": result.watermark,
            "receipts": [receipt_to_dict(op, replayed=replayed) for op, replayed in result.receipts],
        }
        any_new = any(not replayed for _, replayed in result.receipts)
        return Response(
            IngestBatchResponseSerializer(body).data,
            status=status.HTTP_201_CREATED if any_new else status.HTTP_200_OK,
        )


class IngestMediaViewSet(viewsets.mixins.CreateModelMixin,
                          viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    采集媒体暂存：multipart 上传 `media_id` + `device_id` + `image`。
    按 media_id 幂等——上传中断后重传相同内容返回原记录（reused=true）；
    内容不一致返回 409。媒体落盘后会唤醒等待它的待处理操作。
    """

    queryset = IngestMedia.objects.all()
    serializer_class = IngestMediaSerializer
    filterset_fields = ["device_id", "media_id"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        media, created = store_media(
            device_id=serializer.validated_data["device_id"],
            media_id=serializer.validated_data["media_id"],
            image_file=serializer.validated_data["image"],
        )
        media._reused = not created
        return Response(
            self.get_serializer(media).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class IngestOperationViewSet(viewsets.mixins.RetrieveModelMixin,
                             viewsets.mixins.ListModelMixin,
                             viewsets.GenericViewSet):
    """接收账本与逐条回执的状态查询（只读）+ 单条重试动作。"""

    queryset = IngestOperation.objects.select_related("receipt").all()
    serializer_class = IngestOperationReadSerializer
    filterset_fields = ["device_id", "op_type", "receipt__status"]

    @extend_schema(request=None, responses=IngestReceiptSerializer)
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """
        人工重试：rejected/failed 重置为待处理后按序重派；pending 触发一次处理循环；
        已 processed 返回 409（重放请走批量接入，不能重复执行）。
        """
        op = self.get_object()
        receipt = retry_operation(op.id)
        return Response(IngestReceiptSerializer(receipt_to_dict(receipt.operation)).data)


class IngestDeviceViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """设备水位：已连续接收/连续处理序号与各状态计数。"""

    queryset = DeviceWatermark.objects.all()
    serializer_class = DeviceWatermarkSerializer
    lookup_field = "device_id"
