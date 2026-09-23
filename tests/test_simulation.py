"""Realistic, fluctuating simulations driving the real Arbitrator (not a
hand-copied mirror of main.py's logic) -- covering convergence, asymmetric
demand, overshoot, and WAN failover behavior under conditions closer to
the real world than the idealized "speed instantly equals its assigned
limit" used elsewhere: ramp-up lag, imprecise rate limiting, and random
jitter.
"""
import random

from bandwidtharr.allocator import Arbitrator

TOTAL = 100_000_000.0  # 100 MB/s ~ 800 Mbps
ACTIVE_THRESHOLD = 250_000.0  # 2 Mbps-equivalent
POLL_INTERVAL = 3.0
SETTLE_SECONDS = 30.0


class SimulatedApp:
    """Models one app's real-world download speed under a given assigned
    limit.

    - true_max: the app's actual achievable ceiling right now -- a plain
      float, or a callable(cycle) -> float for a ceiling that changes
      over time (more torrent peers connect, an NZB job finishes).
    - overshoot_factor: how much real speed exceeds the assigned limit
      before being capped at true_max (qBittorrent's imprecise rate
      limiting; 1.0 for an app that obeys its cap exactly).
    - ramp_lag_cycles: speed moves only 1/ramp_lag_cycles of the way
      toward its target each cycle (TCP/uTP ramp-up), not instantly.
    - noise_fraction: +/- random jitter applied each cycle (0 = perfectly
      smooth curves).
    """

    def __init__(self, true_max, overshoot_factor=1.0, ramp_lag_cycles=1, noise_fraction=0.0, rng=None):
        self._true_max = true_max
        self.overshoot_factor = overshoot_factor
        self.ramp_lag_cycles = max(1, ramp_lag_cycles)
        self.noise_fraction = noise_fraction
        self.rng = rng or random.Random(0)
        self.speed = 0.0

    def true_max_at(self, cycle: int) -> float:
        return self._true_max(cycle) if callable(self._true_max) else self._true_max

    def step(self, limit: float, cycle: int) -> float:
        target = min(self.true_max_at(cycle), limit * self.overshoot_factor)
        self.speed += (target - self.speed) / self.ramp_lag_cycles
        if self.noise_fraction:
            self.speed *= 1 + self.rng.uniform(-self.noise_fraction, self.noise_fraction)
        self.speed = max(0.0, self.speed)
        return self.speed


def run_simulation(total, qbit_app, sab_app, cycles, total_schedule=None, settle_seconds=SETTLE_SECONDS):
    """Drive a real Arbitrator for `cycles` simulated poll cycles against
    the two SimulatedApps. `total_schedule` is an optional {cycle: new_total}
    map for WAN failover events. Returns (arbitrator, history), where
    history is a list of (qbit_limit, sab_limit, qbit_speed, sab_speed)
    per cycle."""
    arbitrator = Arbitrator(total)
    now = 0.0
    current_total = total
    history = []

    for cycle in range(1, cycles + 1):
        link_changed = False
        if total_schedule and cycle in total_schedule:
            current_total = total_schedule[cycle]
            link_changed = True

        new_qbit, qbit_apply, new_sab, sab_apply, _penalty = arbitrator.step(
            now, qbit_app.speed, sab_app.speed, current_total, ACTIVE_THRESHOLD, settle_seconds, link_changed,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab

        qbit_speed = qbit_app.step(arbitrator.qbit_limit, cycle)
        sab_speed = sab_app.step(arbitrator.sab_limit, cycle)
        history.append((arbitrator.qbit_limit, arbitrator.sab_limit, qbit_speed, sab_speed))
        now += POLL_INTERVAL

    return arbitrator, history


def test_both_hungry_with_realistic_fluctuation_converges_and_holds():
    # The original reported bug (runaway skew), now under realistic ramp
    # lag, mild qBittorrent overshoot, and random jitter instead of
    # idealized instant tracking.
    rng = random.Random(42)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.05, ramp_lag_cycles=4, noise_fraction=0.03, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.02, rng=rng)
    qbit.speed = TOTAL * 0.06
    sab.speed = TOTAL * 0.85

    _arbitrator, history = run_simulation(TOTAL, qbit, sab, cycles=150)
    tail = history[-30:]
    diffs = [abs(q - s) for q, s, _, _ in tail]
    combined = [qs + ss for _, _, qs, ss in tail]
    assert max(diffs) <= TOTAL * 0.06
    assert max(combined) <= TOTAL * 1.08


def test_genuinely_asymmetric_demand_settles_near_true_need():
    rng = random.Random(1)
    qbit = SimulatedApp(true_max=TOTAL * 0.15, overshoot_factor=1.0, ramp_lag_cycles=3, noise_fraction=0.02, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.02, rng=rng)
    qbit.speed = TOTAL * 0.02
    sab.speed = TOTAL

    arbitrator, history = run_simulation(TOTAL, qbit, sab, cycles=60)
    assert arbitrator.qbit_limit < TOTAL * 0.25
    assert arbitrator.sab_limit > arbitrator.qbit_limit


def test_demand_recovery_after_slack_climbs_back_to_fair_share():
    rng = random.Random(2)
    qbit = SimulatedApp(true_max=TOTAL * 0.15, overshoot_factor=1.0, ramp_lag_cycles=3, noise_fraction=0.02, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.02, rng=rng)
    qbit.speed = TOTAL * 0.02
    sab.speed = TOTAL

    arbitrator = Arbitrator(TOTAL)
    now = 0.0
    for cycle in range(1, 251):
        if cycle == 31:
            qbit._true_max = TOTAL  # more torrent peers connect
        new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
            now, qbit.speed, sab.speed, TOTAL, ACTIVE_THRESHOLD, SETTLE_SECONDS, False,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab
        qbit.step(arbitrator.qbit_limit, cycle)
        sab.step(arbitrator.sab_limit, cycle)
        now += POLL_INTERVAL

    assert abs(arbitrator.qbit_limit - arbitrator.sab_limit) <= TOTAL * 0.02


def test_persistent_overshoot_stays_bounded_and_sab_does_not_wander():
    # Regression coverage for the fair-share/overshoot decoupling fix:
    # sab's share should stay essentially stable even while qBittorrent
    # persistently overshoots its assigned cap.
    rng = random.Random(3)
    qbit = SimulatedApp(true_max=TOTAL * 1.1, overshoot_factor=1.10, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL * 1.1, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    _arbitrator, history = run_simulation(TOTAL, qbit, sab, cycles=150)
    tail = history[-60:]
    sab_limits = [s for _, s, _, _ in tail]
    combined = [qs + ss for _, _, qs, ss in tail]
    assert max(sab_limits) - min(sab_limits) <= TOTAL * 0.005  # sab barely moves
    assert max(combined) <= TOTAL * 1.08  # overshoot stays bounded, not runaway


def test_noise_alone_does_not_cause_runaway():
    # Pure random jitter, no systematic overshoot or ramp lag bias -- the
    # split should stay bounded, not drift or oscillate wildly just from
    # measurement noise.
    rng = random.Random(4)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=1, noise_fraction=0.1, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=1, noise_fraction=0.1, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    _arbitrator, history = run_simulation(TOTAL, qbit, sab, cycles=150)
    tail = history[-60:]
    combined = [qs + ss for _, _, qs, ss in tail]
    assert max(combined) <= TOTAL * 1.15


def test_wan_failover_to_lower_budget_reaches_sensible_split_promptly():
    rng = random.Random(5)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    backup_total = TOTAL * 0.0625  # e.g. 50 of 800 Mbps
    arbitrator, history = run_simulation(
        TOTAL, qbit, sab, cycles=15, total_schedule={5: backup_total},
    )
    # right at the failover cycle, both sides should already reflect the new,
    # much smaller budget -- not still carrying primary-scale numbers.
    post_failover = history[4]  # cycle 5, 0-indexed
    qbit_limit, sab_limit, _, _ = post_failover
    assert qbit_limit + sab_limit <= backup_total + 1
    assert qbit_limit == sab_limit == round(backup_total / 2)


def test_recovery_from_backup_to_primary_is_immediate_not_stuck_for_minutes():
    # Regression test for the bug found while building this suite:
    # recovering from a converged small-budget split to a much larger
    # total used to force a spurious jump (an artifact of allocate()'s
    # defensive clamp, not a fairness decision) and then take several
    # minutes to climb back to a fair split. Should now be immediate.
    rng = random.Random(6)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=1, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=1, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    backup_total = TOTAL * 0.0625
    arbitrator, history = run_simulation(
        TOTAL, qbit, sab, cycles=15, total_schedule={2: backup_total, 10: TOTAL},
    )
    post_recovery = history[9]  # cycle 10, 0-indexed -- the recovery cycle itself
    qbit_limit, sab_limit, _, _ = post_recovery
    assert qbit_limit == sab_limit == round(TOTAL / 2)
    # and it holds there afterward, rather than drifting from an artifact
    for qbit_limit, sab_limit, _, _ in history[9:]:
        assert abs(qbit_limit - sab_limit) <= TOTAL * 0.01


def test_wan_failover_with_active_overshoot_correction_does_not_pin_qbit_at_floor():
    # Regression: with a standing overshoot correction built up at primary
    # scale, failing over to a much smaller backup budget used to pin
    # qBittorrent at its MIN_SHARE_FRACTION floor for minutes -- both the
    # carried-over penalty itself (sized against the OLD total) and the
    # huge transient overshoot while both apps' speeds ramp down from
    # primary levels to the new budget independently exceed any correction
    # the small backup budget can absorb. While floor-pinned (penalty
    # decays at only 1% of the total per cycle), qBittorrent also reads as
    # "slack" to allocate(), so fairness hands its share to SABnzbd
    # permanently. Simulation verified: without reset-on-link-change plus
    # a per-cycle penalty growth cap, qbit stays at the floor for the
    # entire window and the split ends 0.625/4.94 instead of ~50/50.
    rng = random.Random(3)
    qbit = SimulatedApp(true_max=TOTAL * 1.1, overshoot_factor=1.10, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL * 1.1, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    backup_total = TOTAL * 0.0625  # e.g. 50 of 800 Mbps
    failover_cycle = 30
    arbitrator = Arbitrator(TOTAL)
    now = 0.0
    penalty_at_failover = None
    post_failover = []  # (qbit_limit, combined_speed) from the failover cycle onward

    current_total = TOTAL
    for cycle in range(1, failover_cycle + 41):
        link_changed = cycle == failover_cycle
        if link_changed:
            current_total = backup_total
            penalty_at_failover = arbitrator.overshoot_compensator.penalty

        new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
            now, qbit.speed, sab.speed, current_total, ACTIVE_THRESHOLD, SETTLE_SECONDS, link_changed,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab
        qbit_speed = qbit.step(arbitrator.qbit_limit, cycle)
        sab_speed = sab.step(arbitrator.sab_limit, cycle)
        if cycle >= failover_cycle:
            post_failover.append((arbitrator.qbit_limit, qbit_speed + sab_speed))
        now += POLL_INTERVAL

    # scenario premise: there really is a standing, primary-scale correction
    # in effect at the moment of failover -- without this, the test proves
    # nothing about carrying one over
    assert penalty_at_failover > TOTAL * 0.01

    # qbit's applied limit never collapses toward the floor (a brief dip a
    # bit under its fair half-share during the ramp-down transient is
    # healthy and self-correcting -- what must not happen is the
    # floor-pinning that leaves it at MIN_SHARE_FRACTION for minutes)
    qbit_limits = [ql for ql, _ in post_failover]
    assert min(qbit_limits) >= backup_total * 0.25

    # the ramp-down transient is brief -- no sustained stretch of the
    # backup budget going unused
    wasted = sum(1 for _ql, combined in post_failover if combined < backup_total * 0.8)
    assert wasted <= 2

    # and it ends at a roughly even split, not permanently skewed to SAB
    assert abs(arbitrator.qbit_limit - arbitrator.sab_limit) <= backup_total * 0.05


def test_qbit_outage_freezes_state_then_resumes_correctly():
    # A real network/API outage means main.py simply doesn't call
    # Arbitrator.step() for however many cycles qbit is unreachable
    # (mirrors main.py's `if qbit_ok and sab_ok:` gate) -- state should
    # stay exactly frozen through the outage, then resume normally
    # afterward with no crash or corrupted state. (main.py's long-outage
    # budget handover deliberately applies limits straight to the
    # reachable app's API and never touches the Arbitrator, so this
    # still holds -- see test_long_peer_outage_hands_full_budget_...)
    rng = random.Random(7)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    arbitrator = Arbitrator(TOTAL)
    now = 0.0

    def run_cycles(n):
        nonlocal now
        for cycle in range(1, n + 1):
            new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
                now, qbit.speed, sab.speed, TOTAL, ACTIVE_THRESHOLD, SETTLE_SECONDS, False,
            )
            if qbit_apply:
                arbitrator.qbit_limit = new_qbit
            if sab_apply:
                arbitrator.sab_limit = new_sab
            qbit.step(arbitrator.qbit_limit, cycle)
            sab.step(arbitrator.sab_limit, cycle)
            now += POLL_INTERVAL

    run_cycles(20)
    frozen_qbit_limit = arbitrator.qbit_limit
    frozen_sab_limit = arbitrator.sab_limit
    frozen_fair_share = arbitrator.qbit_fair_share

    # qbit unreachable for ~1 minute -- main.py wouldn't call step() at all
    for _ in range(20):
        now += POLL_INTERVAL

    assert arbitrator.qbit_limit == frozen_qbit_limit
    assert arbitrator.sab_limit == frozen_sab_limit
    assert arbitrator.qbit_fair_share == frozen_fair_share

    # qbit comes back -- resumes normally
    run_cycles(20)
    assert arbitrator.qbit_limit + arbitrator.sab_limit <= TOTAL + 1
    assert arbitrator.qbit_limit > 0
    assert arbitrator.sab_limit > 0


def _run_peer_outage(
    down, down_from, down_to, cycles, peer_keeps_transferring,
    total=TOTAL, total_schedule=None, sync_tracked_limit=True,
):
    """Mirror main.py's peer-outage handover against the real Arbitrator and
    the real should_hand_out_budget(): step() only while both apps are
    reachable; while `down` ("qbit" or "sab") is unreachable, hand the
    survivor the full current budget once the outage outlasts the window,
    updating the Arbitrator's tracked applied value exactly as main.py does.

    `peer_keeps_transferring` models the realistic binhex case -- the API is
    blind but the app's transfers keep running at the last limit it had
    applied -- versus the container itself being down (speed 0).
    `total_schedule` ({cycle: new_total}) models WAN link flips, including
    ones confirmed mid-outage, which main.py keeps pending until a step()
    consumes them. `sync_tracked_limit=False` reproduces the pre-fix
    main.py for the regression test. Returns (arbitrator, qbit, sab, trace)
    with trace as {cycle: (applied_qbit, applied_sab, qbit_speed,
    sab_speed)}."""
    from bandwidtharr.main import PEER_HANDOVER_SECONDS, should_hand_out_budget

    rng = random.Random(11)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = total * 0.5
    sab.speed = total * 0.5

    arbitrator = Arbitrator(total)
    now = 0.0
    current_total = total
    unreachable_since = None
    pending_link_changed = False
    # the API-applied values, which pre-fix main.py let drift from the
    # Arbitrator's tracked ones during a handover
    applied_qbit, applied_sab = arbitrator.qbit_limit, arbitrator.sab_limit
    trace = {}

    for cycle in range(1, cycles + 1):
        peer_ok = not (down_from <= cycle < down_to)
        if peer_ok:
            unreachable_since = None
        elif unreachable_since is None:
            unreachable_since = now
        if total_schedule and cycle in total_schedule:
            current_total = total_schedule[cycle]
            pending_link_changed = True

        if peer_ok:
            new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
                now, qbit.speed, sab.speed, current_total, ACTIVE_THRESHOLD, SETTLE_SECONDS, pending_link_changed,
            )
            pending_link_changed = False
            if qbit_apply:
                arbitrator.qbit_limit = applied_qbit = new_qbit
            if sab_apply:
                arbitrator.sab_limit = applied_sab = new_sab
        elif should_hand_out_budget(now, unreachable_since, PEER_HANDOVER_SECONDS):
            if down == "sab":
                applied_qbit = current_total
                if sync_tracked_limit:
                    arbitrator.qbit_limit = current_total
            else:
                applied_sab = current_total
                if sync_tracked_limit:
                    arbitrator.sab_limit = current_total

        idle = None
        if not peer_ok and not peer_keeps_transferring:
            idle = qbit if down == "qbit" else sab  # container itself down, not just its API
        for app, applied in ((qbit, applied_qbit), (sab, applied_sab)):
            if app is idle:
                app.speed = 0.0
            else:
                app.step(applied, cycle)
        trace[cycle] = (applied_qbit, applied_sab, qbit.speed, sab.speed)
        now += POLL_INTERVAL

    return arbitrator, qbit, sab, trace


def test_long_peer_outage_hands_full_budget_to_reachable_app_then_resumes_fair():
    # Regression for the frozen-split gap: with one app's API down for
    # longer than the handover window, main.py hands the reachable app the
    # full effective budget. Short blips must NOT hand over (covered by the
    # window check below).
    from bandwidtharr.main import PEER_HANDOVER_SECONDS

    sab_down_from, sab_down_to = 21, 56
    arbitrator, qbit, sab, trace = _run_peer_outage(
        "sab", sab_down_from, sab_down_to, cycles=130, peer_keeps_transferring=False,
    )

    # blip tolerance: no handover the moment the outage starts...
    assert trace[sab_down_from + 1][0] == round(TOTAL / 2)
    # ...but once the outage has clearly outlasted the window, the full
    # budget is handed over and actually used
    handover_cycle = sab_down_from + int(PEER_HANDOVER_SECONDS / POLL_INTERVAL)
    assert trace[handover_cycle + 2][0] == TOTAL
    assert trace[sab_down_to - 1][2] >= TOTAL * 0.9

    # re-entry: qbit already at ceiling reads the returning app as idle
    # (both get the ceiling -- allocate()'s own designed behavior), so the
    # transient double-ceiling burst builds a compensator penalty that then
    # decays; converges to an even split well within the run and holds
    # there -- strictly within-budget-bound once both are genuinely active.
    assert abs(arbitrator.qbit_limit - arbitrator.sab_limit) <= TOTAL * 0.06
    assert qbit.speed + sab.speed <= TOTAL * 1.06


def test_peer_returning_mid_transfer_is_reined_in_on_the_first_resumed_cycle():
    # Regression for a real bug in the handover: it set the survivor's
    # limit via the API but never updated the Arbitrator's tracked applied
    # value. That only happened to work when the peer came back idle. In
    # the realistic case -- its API was blind but its transfers kept
    # running at the old limit -- the first resumed cycle compared the
    # survivor's fair share against the stale tracked value, saw "no
    # change", and left it at the FULL budget for ~8 cycles (~24s at the
    # default poll) at ~150% of budget, while the overshoot compensator
    # squeezed the app that was NOT over. Both outage directions.
    down_from, down_to = 21, 56
    for down in ("sab", "qbit"):
        arbitrator, _qbit, _sab, trace = _run_peer_outage(
            down, down_from, down_to, cycles=90, peer_keeps_transferring=True,
        )
        survivor_idx = 0 if down == "sab" else 1
        assert trace[down_to - 1][survivor_idx] == TOTAL, down  # premise: handover happened
        assert trace[down_to][survivor_idx] < TOTAL, f"{down}: survivor not reined in on the resumed cycle"
        peak = max(qs + ss for _, _, qs, ss in (trace[c] for c in range(down_to, down_to + 10)))
        assert peak <= TOTAL * 1.3, f"{down}: combined peaked at {peak / TOTAL:.0%} of budget after resume"
        assert abs(arbitrator.qbit_limit - arbitrator.sab_limit) <= TOTAL * 0.06, down

    # and the pre-fix behaviour really does fail this, so the test is
    # actually guarding something
    _a, _q, _s, stale = _run_peer_outage(
        "qbit", down_from, down_to, cycles=90, peer_keeps_transferring=True, sync_tracked_limit=False,
    )
    assert stale[down_to][1] == TOTAL
    assert max(qs + ss for _, _, qs, ss in (stale[c] for c in range(down_to, down_to + 10))) > TOTAL * 1.4


def test_link_flip_during_outage_delivers_pending_link_changed_on_resume():
    # The link-check runs even while arbitration is skipped (one app's API
    # down), so a confirmed link flip can happen in a cycle where step() is
    # never called -- main.py must remember it (pending flag) and deliver
    # link_changed=True on the first resumed cycle. Without that, the
    # Arbitrator resumes against the new budget with old-scale bookkeeping
    # and allocate()'s defensive floor clamp forces the documented spurious
    # jump (e.g. a converged backup split resuming on primary forces
    # qbit to the 10% floor and hands 90% to SAB) instead of classifying
    # both apps from a neutral half-of-new-total baseline.
    backup_total = TOTAL * 0.0625
    sab_down_from, sab_down_to, link_flip_cycle = 21, 51, 31
    _arbitrator, _qbit, _sab, trace = _run_peer_outage(
        "sab", sab_down_from, sab_down_to, cycles=61, peer_keeps_transferring=True,
        total=backup_total, total_schedule={link_flip_cycle: TOTAL},
    )

    # premise: qbit was handed the flipped-to (primary) budget mid-outage...
    assert trace[link_flip_cycle + 15][0] == TOTAL
    # ...and at resume both apps were genuinely active at old backup scale
    # (sab still pulling ~3 Mbps) with qbit demanding primary scale --
    # otherwise the stale bookkeeping has nothing to get wrong
    resumed_input_speeds = trace[sab_down_to - 1][2:]
    assert ACTIVE_THRESHOLD < resumed_input_speeds[1] < backup_total * 1.01
    assert resumed_input_speeds[0] > TOTAL * 0.9

    # The resumed cycle must apply a decision made against the NEW budget
    # from the neutral half-reset baseline: qbit gets what it's actually
    # demanding (primary scale minus sab's demonstrated share+headroom).
    # Without the pending flag, the stale backup-scale bookkeeping makes
    # allocate()'s defensive floor clamp force the documented artifact --
    # qbit to the 10% floor with 90% handed to a barely-downloading SAB.
    resumed = trace[sab_down_to][:2]
    assert resumed[0] > TOTAL * 0.7, "qbit should follow its real demand, not the stale-scale clamp"
    assert resumed[1] <= TOTAL * 0.2 + 1, "sab keeps demonstrated speed plus headroom"


def test_wan_link_flapping_stays_sane_no_runaway():
    # An unstable backup link (e.g. Starlink dropping in and out) can make
    # the router bounce between primary and backup repeatedly. Each
    # confirmed flap forces a link_changed reset -- confirm this never
    # produces an out-of-bounds result or blows up, however often it flaps.
    rng = random.Random(8)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    backup_total = TOTAL * 0.0625
    arbitrator = Arbitrator(TOTAL)
    now = 0.0
    current_total = TOTAL
    for cycle in range(1, 61):
        link_changed = cycle % 5 == 0  # flap every 5 cycles (~15s)
        if link_changed:
            current_total = backup_total if current_total == TOTAL else TOTAL
        new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
            now, qbit.speed, sab.speed, current_total, ACTIVE_THRESHOLD, SETTLE_SECONDS, link_changed,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab
        qbit.step(arbitrator.qbit_limit, cycle)
        sab.step(arbitrator.sab_limit, cycle)
        assert arbitrator.qbit_limit >= 0
        assert arbitrator.sab_limit >= 0
        assert arbitrator.qbit_limit + arbitrator.sab_limit <= current_total + 1
        now += POLL_INTERVAL


def test_flaky_link_detector_blips_are_debounced_before_reaching_arbitrator():
    # A noisy/flaky link detector (e.g. a transient DNS hiccup misreading
    # an ASN lookup) shouldn't be able to trigger a real budget swap on its
    # own -- LinkStateTracker's confirm-count debounce must filter isolated
    # blips out before Arbitrator ever sees link_changed=True.
    from bandwidtharr.link_detector import BACKUP, PRIMARY, LinkStateTracker

    rng = random.Random(9)
    qbit = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    sab = SimulatedApp(true_max=TOTAL, overshoot_factor=1.0, ramp_lag_cycles=2, noise_fraction=0.0, rng=rng)
    qbit.speed = TOTAL * 0.5
    sab.speed = TOTAL * 0.5

    tracker = LinkStateTracker(confirm_count=2)
    arbitrator = Arbitrator(TOTAL)
    now = 0.0

    # mostly PRIMARY readings with isolated single-cycle BACKUP blips --
    # never two BACKUP readings in a row, so the debounce should never flip
    readings = [PRIMARY] * 10
    readings[3] = BACKUP
    readings[7] = BACKUP

    for cycle, reading in enumerate(readings, start=1):
        previous = tracker.confirmed
        confirmed = tracker.observe(reading)
        link_changed = confirmed != previous
        total_now = (TOTAL * 0.0625) if confirmed == BACKUP else TOTAL

        new_qbit, qbit_apply, new_sab, sab_apply, _p = arbitrator.step(
            now, qbit.speed, sab.speed, total_now, ACTIVE_THRESHOLD, SETTLE_SECONDS, link_changed,
        )
        if qbit_apply:
            arbitrator.qbit_limit = new_qbit
        if sab_apply:
            arbitrator.sab_limit = new_sab
        qbit.step(arbitrator.qbit_limit, cycle)
        sab.step(arbitrator.sab_limit, cycle)
        now += POLL_INTERVAL

    assert tracker.confirmed == PRIMARY  # isolated blips never actually confirmed
    assert arbitrator.qbit_limit + arbitrator.sab_limit <= TOTAL + 1
    assert arbitrator.qbit_limit + arbitrator.sab_limit > TOTAL * 0.5  # never dropped to backup scale


def test_flaky_link_detector_two_consecutive_readings_does_confirm():
    # Sanity check the debounce is wired correctly both ways -- two
    # CONSECUTIVE backup readings should genuinely confirm and swap the
    # budget, not just always ignore everything.
    from bandwidtharr.link_detector import BACKUP, PRIMARY, LinkStateTracker

    tracker = LinkStateTracker(confirm_count=2)
    assert tracker.observe(BACKUP) == PRIMARY  # 1st reading -- not yet confirmed
    assert tracker.observe(BACKUP) == BACKUP  # 2nd reading -- confirmed
