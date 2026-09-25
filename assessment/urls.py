from django.urls import include, path
from rest_framework.routers import DefaultRouter

from assessment import views

router = DefaultRouter()
router.register("grids", views.RoadGridViewSet, basename="grid")
router.register("contracts", views.CleaningContractViewSet, basename="contract")
router.register("photos", views.EvidencePhotoViewSet, basename="photo")
router.register("candidates", views.DuplicateCandidateViewSet, basename="candidate")
router.register("events", views.ProblemEventViewSet, basename="event")
router.register("rectifications", views.RectificationViewSet, basename="rectification")
router.register("penalties", views.PenaltyUnitViewSet, basename="penalty")
router.register("penalty-versions", views.PenaltyVersionViewSet, basename="penaltyversion")
router.register("escalations", views.EscalationRecordViewSet, basename="escalation")
router.register("ingest/batches", views.IngestBatchViewSet, basename="ingest-batch")
router.register("ingest/media", views.IngestMediaViewSet, basename="ingest-media")
router.register("ingest/operations", views.IngestOperationViewSet, basename="ingest-operation")
router.register("ingest/devices", views.IngestDeviceViewSet, basename="ingest-device")

urlpatterns = [
    path("", include(router.urls)),
]
