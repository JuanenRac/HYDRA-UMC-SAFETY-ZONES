from hydra_umc_safety_zones.breach import Breach
from hydra_umc_safety_zones.estop import NullEStopRequester, request_estop_for
from hydra_umc_safety_zones.geometry import Point3D
from hydra_umc_safety_zones.zones import ZoneLevel

POS = Point3D(1, 1, 1)


def test_no_requests_when_no_breaches():
    requester = NullEStopRequester()
    requests = request_estop_for((), requester)
    assert requests == ()
    assert requester.sent == []


def test_warning_only_breach_requests_nothing():
    breach = Breach("w1", ZoneLevel.WARNING, "op1", POS)
    requester = NullEStopRequester()
    requests = request_estop_for((breach,), requester)
    assert requests == ()
    assert requester.sent == []


def test_danger_breach_requests_estop_and_is_recorded():
    breach = Breach("d1", ZoneLevel.DANGER, "op1", POS)
    requester = NullEStopRequester()
    requests = request_estop_for((breach,), requester)
    assert len(requests) == 1
    assert requests[0].object_id == "op1"
    assert requests[0].zone_id == "d1"
    assert requester.sent == list(requests)


def test_object_breaching_both_levels_requests_once_for_danger():
    warning = Breach("w1", ZoneLevel.WARNING, "op1", POS)
    danger = Breach("d1", ZoneLevel.DANGER, "op1", POS)
    requester = NullEStopRequester()
    requests = request_estop_for((warning, danger), requester)
    assert len(requests) == 1
    assert requests[0].zone_id == "d1"


def test_only_danger_objects_among_multiple_request_estop():
    safe_warning = Breach("w1", ZoneLevel.WARNING, "safe", POS)
    real_danger = Breach("d1", ZoneLevel.DANGER, "intruder", POS)
    requester = NullEStopRequester()
    requests = request_estop_for((safe_warning, real_danger), requester)
    object_ids = {r.object_id for r in requests}
    assert object_ids == {"intruder"}
