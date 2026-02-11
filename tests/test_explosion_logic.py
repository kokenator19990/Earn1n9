from __future__ import annotations

from src.explosion_monitor import ExplosionMonitor


def test_compute_vr() -> None:
    assert ExplosionMonitor.compute_vr(100.0, 50.0) == 2.0
    assert ExplosionMonitor.compute_vr(0.0, 50.0) == 0.0
    assert ExplosionMonitor.compute_vr(100.0, 0.0) == 0.0


def test_retest_zone_and_rejection() -> None:
    event_high = 100.0
    zone_pct = 0.004
    assert ExplosionMonitor.in_retest_zone(100.0, event_high, zone_pct)
    assert ExplosionMonitor.in_retest_zone(99.7, event_high, zone_pct)
    assert not ExplosionMonitor.in_retest_zone(98.0, event_high, zone_pct)

    fail_drop_pct = 0.006
    assert ExplosionMonitor.is_reject_confirmed(99.4, event_high, fail_drop_pct)
    assert not ExplosionMonitor.is_reject_confirmed(99.6, event_high, fail_drop_pct)


def test_funding_is_weird() -> None:
    assert not ExplosionMonitor.funding_is_weird(0.001, 0.002)
    assert ExplosionMonitor.funding_is_weird(-0.003, 0.002)
    assert not ExplosionMonitor.funding_is_weird(None, 0.002)
