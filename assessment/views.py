"""API 视图。所有写操作都委托给 services 层（领域规则集中、时钟可注入）。"""
from django.db.models import Count, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from assessment.models import (
    CleaningContract,
    CollectionDevice,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    OperationReceipt,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
    StagedMedia,
)
from assessment.serializers import (
    CandidateDecisionSerializer,
    CleaningContractSerializer,
    CollectionDeviceSerializer,
    CreateEventFromPhotoSerializer,
    DuplicateCandidateSerializer,
    EscalationRecordSerializer,
    EscalationRunSerializer,
    EvidencePhotoSerializer,
    IngestBatchResponseSerializer,
    IngestBatchSerializer,
    OperationReceiptSerializer,
    PenaltyCorrectionSerializer,
    PenaltyReviewSerializer,
    PenaltyUnitSerializer,
    PenaltyVersionSerializer,
    ProblemEventSerializer,
    RectificationReadSerializer,
    RectifyRequestSerializer,
    RoadGridSerializer,
    StagedMediaReadSerializer,
    StagedMediaUploadSerializer,
)
from assessment.services.clock import resolve_clock
from assessment.services.decisions import decide_candidate
from assessment.services.escalation import run_escalation
from assessment.services.events import create_event_from_photo
from assessment.services.ingest import ingest_batch, retry_operation, stage_media
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


# ---------------------------------------------------------------- 采集包接入


class IngestMediaViewSet(viewsets.mixins.CreateModelMixin,
                         viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """
    暂存媒体：设备先上传照片文件，再在采集包操作中按 media_key 引用。
    上传按 (设备, media_key) 幂等——中断重传返回原记录（200），同键不同内容 409。
    暂存媒体只读不可改删；被操作成功消费后才转为证据照片。
    """

    queryset = StagedMedia.objects.select_related("device").all()
    filterset_fields = ["status", "device__device_no"]

    def get_serializer_class(self):
        if self.action == "create":
            return StagedMediaUploadSerializer
        return StagedMediaReadSerializer

    @extend_schema(responses=StagedMediaReadSerializer)
    def create(self, request, *args, **kwargs):
        payload = StagedMediaUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        media, created = stage_media(
            device_no=data["device_no"],
            media_key=data["media_key"],
            image_file=data["image"],
        )
        return Response(
            StagedMediaReadSerializer(media).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class IngestBatchView(APIView):
    """采集包批量接入：同一设备的一批操作，逐条入账、按序执行、逐条回执。"""

    @extend_schema(request=IngestBatchSerializer, responses=IngestBatchResponseSerializer)
    def post(self, request):
        payload = IngestBatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = ingest_batch(
            device_no=data["device_no"],
            operations=data["operations"],
            batch_no=data.get("batch_no", ""),
        )
        return Response(result, status=status.HTTP_201_CREATED)


class IngestDeviceViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """采集设备状态：接收/处理水位与各状态回执数量（只读，设备在接入时自动登记）。"""

    lookup_field = "device_no"
    serializer_class = CollectionDeviceSerializer

    def get_queryset(self):
        counts = {
            f"{status}_count": Count("operations__receipt", filter=Q(operations__receipt__status=status))
            for status in ("applied", "pending", "ignored", "failed")
        }
        return CollectionDevice.objects.annotate(**counts)


class IngestReceiptViewSet(viewsets.mixins.RetrieveModelMixin,
                           viewsets.mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    """逐条回执查询（含账本原文）与失败重试。"""

    queryset = OperationReceipt.objects.select_related("operation", "operation__device").all()
    serializer_class = OperationReceiptSerializer
    filterset_fields = [
        "status", "operation__op_type", "operation__batch_no", "operation__device__device_no",
    ]

    @extend_schema(request=None, responses=OperationReceiptSerializer)
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """
        重试失败的回执：复位后从设备水位处重新按序级联执行。
        仅 failed 状态可重试，其余状态 409。
        """
        receipt = retry_operation(self.get_object())
        return Response(OperationReceiptSerializer(receipt).data)
