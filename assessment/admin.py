from django.contrib.gis import admin

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

admin.site.register(RoadGrid, admin.GISModelAdmin)
admin.site.register([CleaningContract, EvidencePhoto, DuplicateCandidate])
admin.site.register([ProblemEvent, Rectification])
admin.site.register([PenaltyUnit, PenaltyVersion, EscalationRecord, ReviewRecord])
admin.site.register([CollectionDevice, StagedMedia, IngestedOperation, OperationReceipt])
