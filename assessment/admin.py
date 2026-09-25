from django.contrib.gis import admin

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

admin.site.register(RoadGrid, admin.GISModelAdmin)
admin.site.register([CleaningContract, EvidencePhoto, DuplicateCandidate])
admin.site.register([ProblemEvent, Rectification])
admin.site.register([PenaltyUnit, PenaltyVersion, EscalationRecord, ReviewRecord])
admin.site.register([DeviceWatermark, IngestOperation, IngestReceipt, IngestMedia])
