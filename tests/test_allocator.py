from bandwidtharr.allocator import Arbitrator, OvershootCompensator, allocate

TOTAL = 100_000_000.0  # 100 MB/s ~ 800 Mbps
ACTIVE = 250_000.0


def run(qbit_speed, sab_speed, qbit_limit=TOTAL, sab_limit=TOTAL):
    return allocate(qbit_speed, sab_speed, qbit_limit, sab_limit, TOTAL, ACTIVE)


def test_both_idle_gets_full_ceiling():
    qbit_limit, sab_limit = run(0, 0)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL


def test_only_qbit_active_gets_full_ceiling():
    # a lone downloader gets the whole budget, not total-minus-floor -- the floor
    # is only there to keep either side from being squeezed to zero once BOTH
    # apps are active, not to pre-reserve headroom for one that isn't running.
    qbit_limit, sab_limit = run(50_000_000, 0)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL


def test_only_sab_active_gets_full_ceiling():
    qbit_limit, sab_limit = run(0, 50_000_000)
    assert qbit_limit == TOTAL
    assert sab_limit == TOTAL


def test_first_both_active_cycle_resets_to_fifty_fifty_not_proportional_demand():
    # Previously, whichever app had ramped up faster (or started earlier)
    # looked "hungrier" from raw instantaneous demand and grabbed an unfair
    # head start on the very first cycle both apps are active. Resetting to
    # an equal baseline instead removes that ramp-order bias entirely --
    # qbit here is measured pulling 3x what sab is, but that's still not
    # enough to earn it more than an equal starting share.
    qbit_limit, sab_limit = run(30_000_000, 10_000_000)
    assert qbit_limit == sab_limit == round(TOTAL / 2)


def test_both_active_equal_demand_splits_evenly():
    qbit_limit, sab_limit = run(20_000_000, 20_000_000)
    assert qbit_limit == sab_limit == round(TOTAL / 2)


def test_slack_app_is_shrunk_in_favor_of_hungry_app():
    # sab has a generous 50M cap but is only using 5M of it (demonstrably
    # slack); qbit is capped low and fully using its 5M cap (hungry). sab's
    # share should shrink toward what it's actually using (plus headroom),
    # handing the difference to qbit.
    qbit_limit, sab_limit = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=5_000_000, sab_limit=50_000_000,
    )
    assert qbit_limit > sab_limit


def test_shares_hold_when_both_comfortably_mid_range():
    # Neither hungry nor slack: shares should be left exactly as they are --
    # neither app is constrained by its cap in this regime, so any move
    # would be reacting to noise rather than demand.
    qbit_limit, sab_limit = run(
        qbit_speed=5_000_000, sab_speed=5_000_000, qbit_limit=50_000_000, sab_limit=50_000_000,
    )
    assert qbit_limit == 50_000_000
    assert sab_limit == 50_000_000


def test_never_negative_or_over_total_on_a_tiny_backup_budget():
    # Regression test for a real bug: with a separate, fixed MIN_FLOOR_MBPS
    # (not auto-scaled), a small BACKUP_TOTAL_LIMIT_MBPS could be smaller
    # than that floor, producing a *negative* limit for one side --
    # main.py had no sanity check before sending that straight to the
    # qBittorrent/SABnzbd APIs. There's no separate floor value left to
    # conflict with `total` now, so this is structurally impossible: the
    # internal minimum share is always a fraction of whatever `total`
    # actually is.
    qbit_limit, sab_limit = allocate(
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
            qbit_limit, sab_limit = run(qs, ss)
            assert qbit_limit + sab_limit <= TOTAL + 1  # rounding slack


def _run_loop(
    qbit_speed, sab_speed, qbit_true_max, sab_true_max, cycles,
    jump_at=None, jump_to=None, link_changed_at=None,
    settle_seconds=30.0, poll_interval=3.0,
):
    """Drive the real Arbitrator (not a hand-copied mirror of main.py's
    logic) through `cycles` simulated poll cycles, letting each app's next
    measured speed track whatever was actually applied, capped at its
    modeled true ceiling."""
    arbitrator = Arbitrator(TOTAL)
    now = 0.0
    for cycle in range(1, cycles + 1):
        if jump_at and cycle == jump_at:
            qbit_true_max = jump_to
        link_changed = link_changed_at is not None and cycle == link_changed_at
        new_qbit, qbit_apply, new_sab, sab_apply, _penalty = arbitrator.step(
            now, qbit_speed, sab_speed, TOTAL, ACTIVE, settle_seconds, link_changed,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab
        qbit_speed = min(qbit_true_max, arbitrator.qbit_limit)
        sab_speed = min(sab_true_max, arbitrator.sab_limit)
        now += poll_interval
    return arbitrator.qbit_limit, arbitrator.sab_limit


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
        qbit_true_max=TOTAL, sab_true_max=TOTAL, cycles=90,
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
        qbit_true_max=TOTAL * 0.15, sab_true_max=TOTAL, cycles=250,
        jump_at=31, jump_to=TOTAL,
    )
    assert abs(qbit_limit - sab_limit) <= TOTAL * 0.05


def test_arbitrator_link_changed_resets_fair_share_to_new_total_half():
    # Regression test for a real bug: without this reset, recovering from a
    # converged split on a small budget to a much larger total made
    # allocate()'s defensive clamp (relative to the NEW total) force a
    # spurious jump completely disconnected from actual demand -- e.g.
    # recovering from 25/25 on a 50 Mbps backup link to an 800 Mbps primary
    # forced qbit up to 80 Mbps immediately, purely because 25 fell below
    # 10% of the new total, not because of any fairness decision.
    arbitrator = Arbitrator(TOTAL)
    arbitrator.qbit_fair_share = 25_000_000.0
    arbitrator.qbit_limit = 25_000_000.0
    arbitrator.sab_limit = 25_000_000.0

    new_qbit, qbit_apply, new_sab, sab_apply, _penalty = arbitrator.step(
        now=1_000.0, qbit_speed=25_000_000.0, sab_speed=25_000_000.0,
        total=TOTAL, active_threshold=ACTIVE, reallocation_settle_seconds=30.0,
        rebaseline=True,
    )
    assert new_qbit == round(TOTAL / 2)
    assert new_sab == round(TOTAL / 2)
    assert qbit_apply
    assert sab_apply


def test_arbitrator_recovers_from_backup_to_primary_immediately():
    # End-to-end regression test for the same bug, driven through the full
    # loop helper: converge on a small backup budget, then swap back to the
    # large primary total -- should reach a fair split on the recovery
    # cycle itself, not several minutes later.
    qbit_limit, sab_limit = _run_loop(
        qbit_speed=TOTAL * 0.5, sab_speed=TOTAL * 0.5,
        qbit_true_max=TOTAL, sab_true_max=TOTAL, cycles=2,
    )
    # (both genuinely hungry on primary; now fail over to a tiny backup budget)
    backup_total = TOTAL * 0.0625  # e.g. 50 of 800 Mbps
    arbitrator = Arbitrator(TOTAL)
    arbitrator.qbit_fair_share = qbit_limit
    arbitrator.qbit_limit = qbit_limit
    arbitrator.sab_limit = sab_limit
    now = 0.0
    new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
        now, TOTAL * 0.5, TOTAL * 0.5, backup_total, ACTIVE, 30.0, rebaseline=True,
    )
    arbitrator.qbit_limit, arbitrator.sab_limit = new_qbit, new_sab
    assert new_qbit == new_sab == round(backup_total / 2)

    # recover back to primary
    now += 3.0
    new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
        now, new_qbit, new_sab, TOTAL, ACTIVE, 30.0, rebaseline=True,
    )
    assert new_qbit == new_sab == round(TOTAL / 2)
    assert qbit_apply and sab_apply


def test_slack_shrink_still_works_on_a_small_backup_budget():
    # Regression test: with a fixed Mbps headroom (the old PROBE_STEP_MBPS),
    # a small BACKUP_TOTAL_LIMIT_MBPS budget could make the headroom larger
    # than an app's entire current share, so `min(current_share, speed +
    # headroom)` always resolved to the unchanged current share -- silently
    # disabling slack-redistribution whenever running on a modest backup
    # link. The internal headroom is now a fraction of `total`, so it
    # scales down automatically instead.
    backup_total = 50_000_000.0  # e.g. a 50 Mbps Starlink/5G failover budget
    qbit_limit, sab_limit = allocate(
        qbit_speed=5_000_000, sab_speed=24_000_000,
        qbit_limit=25_000_000, sab_limit=25_000_000,
        total=backup_total, active_threshold=ACTIVE,
    )
    assert qbit_limit < 25_000_000, "slack app's share should have shrunk"


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


def test_first_cycle_applies_even_when_decision_matches_the_assumed_limits():
    # Regression: the tracked limits start as an assumption (full budget),
    # and applies only happen when the decision differs from the tracked
    # value -- so starting up with at most one app active (decision: full
    # budget for both) never sent anything, leaving a stale manual cap in
    # the app untouched until BOTH apps became active. The first cycle
    # must always apply.
    for qbit_speed, sab_speed in ((0.0, 0.0), (50_000_000.0, 0.0), (0.0, 50_000_000.0)):
        arbitrator = Arbitrator(TOTAL)
        new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
            0.0, qbit_speed, sab_speed, TOTAL, ACTIVE, 30.0, False,
        )
        assert new_qbit == new_sab == TOTAL
        assert qbit_apply and sab_apply, (qbit_speed, sab_speed)

        # ...and only the first: the same decision next cycle is a no-op
        arbitrator.qbit_limit, arbitrator.sab_limit = new_qbit, new_sab
        _q, qbit_apply, _s, sab_apply, _p = arbitrator.step(
            3.0, qbit_speed, sab_speed, TOTAL, ACTIVE, 30.0, False,
        )
        assert not qbit_apply and not sab_apply


def test_overshoot_correction_is_debounced_not_applied_every_cycle():
    # Regression test: before the overshoot-settle gate, ANY nonzero
    # penalty -- whether still growing or merely decaying -- forced
    # qBittorrent's limit to be re-applied on literally every poll cycle,
    # never giving its own rate limiter a stable target to settle into.
    # Drive a sustained overshoot (combined speed persistently above
    # budget) and confirm applies are spaced out, not one per 3s poll.
    arbitrator = Arbitrator(TOTAL)
    now = 0.0
    poll_interval = 3.0
    qbit_speed = 55_000_000.0
    sab_speed = 49_000_000.0  # combined 104M > 100M budget -> persistent overshoot
    apply_count = 0
    penalty_active_cycles = 0
    skipped_while_penalty_active = False

    for _ in range(20):
        new_qbit, qbit_apply, _new_sab, _sab_apply, penalty = arbitrator.step(
            now, qbit_speed, sab_speed, TOTAL, ACTIVE,
            reallocation_settle_seconds=30.0, rebaseline=False,
            overshoot_settle_seconds=15.0,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
            apply_count += 1
        if penalty > 0:
            penalty_active_cycles += 1
            if not qbit_apply:
                skipped_while_penalty_active = True
        now += poll_interval

    assert penalty_active_cycles > 5, "test setup should sustain an active overshoot penalty"
    assert skipped_while_penalty_active, "expected at least one cycle with a nonzero penalty that wasn't re-applied"
    assert apply_count < penalty_active_cycles, "qbit limit should not be re-applied on every cycle the penalty is active"
    assert arbitrator.qbit_limit < TOTAL / 2, "overshoot correction should still have taken effect"
