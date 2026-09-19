from bandwidtharr.allocator import OvershootCompensator, allocate

TOTAL = 100_000_000.0  # 100 MB/s ~ 800 Mbps
ACTIVE = 250_000.0


def run(qbit_speed, sab_speed, qbit_limit=TOTAL, sab_limit=TOTAL):
    return allocate(qbit_speed, sab_speed, qbit_limit, sab_limit, TOTAL, ACTIVE)


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


def test_first_both_active_cycle_resets_to_fifty_fifty_not_proportional_demand():
    # Previously, whichever app had ramped up faster (or started earlier)
    # looked "hungrier" from raw instantaneous demand and grabbed an unfair
    # head start on the very first cycle both apps are active. Resetting to
    # an equal baseline instead removes that ramp-order bias entirely --
    # qbit here is measured pulling 3x what sab is, but that's still not
    # enough to earn it more than an equal starting share.
    qbit_limit, sab_limit, saturating = run(30_000_000, 10_000_000)
    assert qbit_limit == sab_limit == round(TOTAL / 2)


def test_both_active_equal_demand_splits_evenly():
    qbit_limit, sab_limit, saturating = run(20_000_000, 20_000_000)
    assert qbit_limit == sab_limit == round(TOTAL / 2)
    assert saturating is False


def test_slack_app_is_shrunk_in_favor_of_hungry_app():
    # sab has a generous 50M cap but is only using 5M of it (demonstrably
    # slack); qbit is capped low and fully using its 5M cap (hungry). sab's
    # share should shrink toward what it's actually using (plus headroom),
    # handing the difference to qbit.
    qbit_limit, sab_limit, saturating = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=5_000_000, sab_limit=50_000_000,
    )
    assert qbit_limit > sab_limit
    assert saturating is True


def test_saturating_false_when_neither_app_near_its_limit():
    qbit_limit, sab_limit, saturating = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=50_000_000, sab_limit=50_000_000,
    )
    assert saturating is False


def test_never_negative_or_over_total_on_a_tiny_backup_budget():
    # Regression test for a real bug: with a separate, fixed MIN_FLOOR_MBPS
    # (not auto-scaled), a small BACKUP_TOTAL_LIMIT_MBPS could be smaller
    # than that floor, producing a *negative* limit for one side --
    # main.py had no sanity check before sending that straight to the
    # qBittorrent/SABnzbd APIs. There's no separate floor value left to
    # conflict with `total` now, so this is structurally impossible: the
    # internal minimum share is always a fraction of whatever `total`
    # actually is.
    qbit_limit, sab_limit, saturating = allocate(
        qbit_speed=1_000_000, sab_speed=1_000_000,
        qbit_limit=1_000_000, sab_limit=1_000_000,
        total=2_000_000, active_threshold=ACTIVE,
    )
    assert qbit_limit >= 0
    assert sab_limit >= 0
    assert qbit_limit + sab_limit <= 2_000_000


def test_result_never_exceeds_total_when_both_are_active():
    # the budget is only actually enforced once both apps are simultaneously
    # active -- with at most one active, both get a full ceiling by design (an
    # idle app's ceiling doesn't consume real bandwidth, and a lone downloader
    # is meant to get the whole budget, not a share of it).
    for qs in (1_000_000, 50_000_000, 200_000_000):
        for ss in (1_000_000, 50_000_000, 200_000_000):
            qbit_limit, sab_limit, _saturating = run(qs, ss)
            assert qbit_limit + sab_limit <= TOTAL + 1  # rounding slack


def _run_loop(qbit_speed, sab_speed, qbit_true_max, sab_true_max, cycles, jump_at=None, jump_to=None):
    """Simulate main.py's poll loop: call allocate(), apply the result when
    `saturating` bypasses the hysteresis gate (or the change clears it
    normally), then let each app's next measured speed track whatever was
    actually applied, capped at its modeled true ceiling."""
    change_threshold_fraction = 0.05
    qbit_limit = sab_limit = TOTAL
    for cycle in range(1, cycles + 1):
        if jump_at and cycle == jump_at:
            qbit_true_max = jump_to
        new_qbit, new_sab, saturating = allocate(
            qbit_speed, sab_speed, qbit_limit, sab_limit, TOTAL, ACTIVE,
        )
        if saturating or abs(new_qbit - qbit_limit) >= TOTAL * change_threshold_fraction:
            qbit_limit = new_qbit
        if saturating or abs(new_sab - sab_limit) >= TOTAL * change_threshold_fraction:
            sab_limit = new_sab
        qbit_speed = min(qbit_true_max, qbit_limit)
        sab_speed = min(sab_true_max, sab_limit)
    return qbit_limit, sab_limit


def test_both_genuinely_hungry_converges_to_balanced_split_not_runaway_skew():
    # Regression test for a real reported bug: qbit ramped up more slowly
    # than sab at first, producing an initially skewed split. Under the old
    # demand-ratio algorithm this could run away indefinitely instead of
    # correcting -- an app pinned near its own cap looked "hungry" forever,
    # even once it already had far more than a fair share, since the cap
    # kept ~matching whatever it was currently using. Reported live as
    # ~600/200 on an 800 Mbps budget despite BOTH apps independently able to
    # reach the full 800 Mbps when given room -- modeled here the same way.
    qbit_limit, sab_limit = _run_loop(
        qbit_speed=TOTAL * 0.06, sab_speed=TOTAL * 0.90,
        qbit_true_max=TOTAL, sab_true_max=TOTAL, cycles=30,
    )
    assert abs(qbit_limit - sab_limit) <= TOTAL * 0.05


def test_genuinely_slack_app_converges_near_its_true_demand_not_forced_even():
    # qbit's real ceiling (e.g. limited torrent seeders for this content) is
    # much lower than sab's -- the split should settle near qbit's true
    # demand plus headroom, not force an even split regardless of real
    # demand (this redesign targets fairness, not a hardcoded 50/50).
    qbit_true_max = TOTAL * 0.15
    qbit_limit, sab_limit = _run_loop(
        qbit_speed=TOTAL * 0.02, sab_speed=TOTAL,
        qbit_true_max=qbit_true_max, sab_true_max=TOTAL, cycles=30,
    )
    assert qbit_limit < TOTAL * 0.3
    assert qbit_limit >= qbit_true_max
    assert sab_limit > qbit_limit


def test_recovers_when_previously_slack_app_becomes_hungry_again():
    # An app that was shrunk earlier for genuinely having less demand isn't
    # permanently penalized once its demand increases again later (e.g.
    # more torrent peers connect) -- it climbs back to a fair share.
    qbit_limit, sab_limit = _run_loop(
        qbit_speed=TOTAL * 0.02, sab_speed=TOTAL,
        qbit_true_max=TOTAL * 0.15, sab_true_max=TOTAL, cycles=60,
        jump_at=31, jump_to=TOTAL,
    )
    assert abs(qbit_limit - sab_limit) <= TOTAL * 0.05


def test_slack_shrink_still_works_on_a_small_backup_budget():
    # Regression test: with a fixed Mbps headroom (the old PROBE_STEP_MBPS),
    # a small BACKUP_TOTAL_LIMIT_MBPS budget could make the headroom larger
    # than an app's entire current share, so `min(current_share, speed +
    # headroom)` always resolved to the unchanged current share -- silently
    # disabling slack-redistribution whenever running on a modest backup
    # link. The internal headroom is now a fraction of `total`, so it
    # scales down automatically instead.
    backup_total = 50_000_000.0  # e.g. a 50 Mbps Starlink/5G failover budget
    qbit_limit, sab_limit, saturating = allocate(
        qbit_speed=5_000_000, sab_speed=24_000_000,
        qbit_limit=25_000_000, sab_limit=25_000_000,
        total=backup_total, active_threshold=ACTIVE,
    )
    assert qbit_limit < 25_000_000, "slack app's share should have shrunk"
    assert saturating is True


def test_overshoot_compensator_no_penalty_within_margin():
    comp = OvershootCompensator()
    for _ in range(5):
        penalty = comp.update(combined_speed=TOTAL * 1.01, effective_total=TOTAL)
    assert penalty == 0.0


def test_overshoot_compensator_requires_confirm_cycles_before_penalizing():
    comp = OvershootCompensator()
    assert comp.CONFIRM_CYCLES >= 2
    for i in range(comp.CONFIRM_CYCLES - 1):
        penalty = comp.update(combined_speed=TOTAL * 1.10, effective_total=TOTAL)
        assert penalty == 0.0, f"penalized too early, on cycle {i + 1}"
    penalty = comp.update(combined_speed=TOTAL * 1.10, effective_total=TOTAL)
    assert penalty > 0.0


def test_overshoot_compensator_penalty_grows_while_overshoot_persists():
    comp = OvershootCompensator()
    penalties = [comp.update(combined_speed=TOTAL * 1.10, effective_total=TOTAL) for _ in range(6)]
    growing = [b > a for a, b in zip(penalties, penalties[1:]) if a > 0]
    assert any(growing)
    assert penalties == sorted(penalties)


def test_overshoot_compensator_decays_once_back_within_margin():
    comp = OvershootCompensator()
    for _ in range(6):
        comp.update(combined_speed=TOTAL * 1.10, effective_total=TOTAL)
    peak = comp.penalty
    assert peak > 0.0

    penalty = comp.update(combined_speed=TOTAL * 0.9, effective_total=TOTAL)
    assert 0.0 <= penalty < peak


def test_overshoot_compensator_decay_never_goes_negative():
    comp = OvershootCompensator()
    for _ in range(50):
        penalty = comp.update(combined_speed=TOTAL * 0.5, effective_total=TOTAL)
    assert penalty == 0.0


def test_overshoot_compensator_penalty_never_exceeds_effective_total():
    comp = OvershootCompensator()
    for _ in range(200):
        penalty = comp.update(combined_speed=TOTAL * 5, effective_total=TOTAL)
    assert penalty <= TOTAL
