"""合同责任归属：按“事件发生时刻”所在的合同区间解析，与录入时间无关。"""
from django.contrib.gis.geos import Point

from assessment.exceptions import AmbiguousContract, NoContractFound
from assessment.models import CleaningContract, RoadGrid


def find_grid(point: Point) -> RoadGrid | None:
    """点落在哪个网格内（PostGIS 空间查询）。"""
    return RoadGrid.objects.filter(geom__covers=point).first()


def responsible_contract(*, point: Point, at) -> CleaningContract:
    """
    解析某位置在某时刻的责任合同：
      valid_from <= 发生时刻 < valid_to
    找不到 -> NoContractFound(422)；多个重叠 -> AmbiguousContract(409)。
    """
    qs = CleaningContract.objects.filter(
        grid__geom__covers=point,
        valid_from__lte=at,
        valid_to__gt=at,
    )
    contracts = list(qs[:2])
    if not contracts:
        raise NoContractFound()
    if len(contracts) > 1:
        raise AmbiguousContract()
    return contracts[0]
