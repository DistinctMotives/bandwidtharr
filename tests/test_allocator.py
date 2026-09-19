from bandwidtharr.allocator import allocate

TOTAL = 100_000_000.0  # 100 MB/s ~ 800 Mbps
FLOOR = 5_000_000.0
ACTIVE = 250_000.0
PROBE = 5_000_000.0


def run(qbit_speed, sab_speed, qbit_limit=TOTAL, sab_limit=TOTAL):
    return allocate(qbit_speed, sab_speed, qbit_limit, sab_limit, TOTAL, FLOOR, ACTIVE, PROBE)


def test_both_idle_gets_full_ceiling():
    qbit_limit, sab_limit, saturating = run(0, 0)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL
    assert saturating is False


def test_only_qbit_active_gets_full_ceiling():
    # a lone downloader gets the whole budget, not total-minus-floor -- the floor
    # is only there to keep either side from being squeezed to zero once BOTH
    # apps are active, not to pre-reserve headroom for one that isn't running.
    qbit_limit, sab_limit, saturating = run(50_000_000, 0)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL
    assert saturating is False


def test_only_sab_active_gets_full_ceiling():
    qbit_limit, sab_limit, saturating = run(0, 50_000_000)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL
    assert saturating is False


def test_both_active_unsaturated_splits_proportional_to_demand():
    # qbit pulling 3x what sab is, neither saturating its (generous) limit
    qbit_limit, sab_limit, saturating = run(30_000_000, 10_000_000)
    assert qbit_limit + sab_limit == TOTAL
    assert qbit_limit == round(TOTAL * 0.75)
    assert sab_limit == round(TOTAL * 0.25)
    assert saturating is False


def test_both_active_equal_demand_splits_evenly():
    qbit_limit, sab_limit, saturating = run(20_000_000, 20_000_000)
    assert qbit_limit == sab_limit == round(TOTAL / 2)
    assert saturating is False


def test_saturated_app_is_treated_as_wanting_more_not_pinned():
    # qbit is capped low and fully using its cap -> should be estimated as hungry
    # (limit + probe), growing its share instead of being read as low true demand.
    qbit_limit, sab_limit, saturating = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=5_000_000, sab_limit=50_000_000,
    )
    # qbit's estimated demand (limit+probe=10M) > sab's raw demand (5M) since sab
    # isn't saturating its own (generous) limit -> qbit should get the larger share
    assert qbit_limit > sab_limit
    assert saturating is True


def test_saturating_false_when_neither_app_near_its_limit():
    qbit_limit, sab_limit, saturating = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=50_000_000, sab_limit=50_000_000,
    )
    assert saturating is False


def test_never_exceeds_total_when_floors_would_overflow():
    # contrived tiny total where both floors alone would exceed it
    qbit_limit, sab_limit, saturating = allocate(
        qbit_speed=1_000_000, sab_speed=1_000_000,
        qbit_limit=1_000_000, sab_limit=1_000_000,
        total=6_000_000, min_floor=5_000_000, active_threshold=ACTIVE, probe_step=PROBE,
    )
    assert qbit_limit + sab_limit <= 6_000_000


def test_result_never_exceeds_total_when_both_are_active():
    # the budget is only actually enforced once both apps are simultaneously
    # active -- with at most one active, both get a full ceiling by design (an
    # idle app's ceiling doesn't consume real bandwidth, and a lone downloader
    # is meant to get the whole budget, not a share of it).
    for qs in (1_000_000, 50_000_000, 200_000_000):
        for ss in (1_000_000, 50_000_000, 200_000_000):
            qbit_limit, sab_limit, _saturating = run(qs, ss)
            assert qbit_limit + sab_limit <= TOTAL + 1  # rounding slack


def test_demand_probing_converges_instead_of_deadlocking_on_hysteresis():
    # Regression test for a real deadlock: qbit just started (still ramping,
    # low measured speed) while sab is already maxed out. The first "both
    # active" cycle reads this as heavily skewed demand, producing a skewed
    # split. Each subsequent correction step is small enough to fall under a
    # typical change-threshold hysteresis gate -- so a caller that only
    # applies changes clearing that threshold would see the *exact* same
    # under-threshold correction computed forever, since unapplied limits
    # never change, so next cycle's inputs (and therefore output) are
    # identical. A caller that also bypasses the gate whenever `saturating`
    # is True (as main.py does) must actually converge instead.
    change_threshold_fraction = 0.05
    qbit_limit = sab_limit = TOTAL
    qbit_speed = TOTAL * 0.095  # ramping torrent, still slow
    sab_speed = TOTAL * 0.9175  # already near max

    for _ in range(40):
        new_qbit, new_sab, saturating = allocate(
            qbit_speed, sab_speed, qbit_limit, sab_limit, TOTAL, FLOOR, ACTIVE, PROBE,
        )
        if saturating or abs(new_qbit - qbit_limit) >= TOTAL * change_threshold_fraction:
            qbit_limit = new_qbit
        if saturating or abs(new_sab - sab_limit) >= TOTAL * change_threshold_fraction:
            sab_limit = new_sab
        # both apps assumed capable of using all headroom given -- pinned at
        # whatever is currently really enforced, same as real qBittorrent/SABnzbd
        # tracking a freshly-applied API limit.
        qbit_speed, sab_speed = qbit_limit, sab_limit

    # Started at a ~9.5/90 skew; should now be much closer to balanced --
    # nowhere near stuck at (or near) the starting point.
    assert qbit_limit >= TOTAL * 0.35
    assert sab_limit <= TOTAL * 0.65
