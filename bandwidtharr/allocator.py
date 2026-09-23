"""Pure bandwidth-allocation logic, kept free of I/O so it's cheap to unit test."""

# Both the headroom given to a shrinking app's share, and the minimum
# share either app can be left with -- as a fraction of the current
# budget, not a fixed absolute value. Scaling with whatever total is in
# effect (primary or backup) avoids two problems a fixed Mbps value would
# have: it could be large enough to swallow an entire small backup-link
# budget (silently disabling the slack-redistribution below), or a fixed
# minimum-share value could itself exceed a small backup budget outright
# (producing a negative "share" for the other app -- a real bug this
# constant replaces the fix for).
MIN_SHARE_FRACTION = 0.1


def allocate(
    qbit_speed: float,
    sab_speed: float,
    qbit_limit: float,
    sab_limit: float,
    total: float,
    active_threshold: float,
) -> tuple[int, int]:
    """Return (qbit_limit, sab_limit) in the same unit as the inputs
    (bytes/sec).

    Max-min fair-share allocation: both apps get an equal split by default,
    and share only moves between them once one side demonstrably isn't
    using what it already has.

    - Whenever at most one app is active, both get the full budget as a ceiling --
      a lone downloader gets all of it, and an idle app stays uncapped so it can
      ramp immediately if it starts (at the cost of a brief, one-poll-interval
      overshoot above budget right when the second app kicks in).
    - The first cycle both apps are simultaneously active (both current
      limits still at the ceiling from the branch above), the baseline
      resets to `total / 2` each -- not whatever the ceiling happened to
      leave them at. Otherwise whichever app ramped up to speed first would
      look artificially "hungrier" than one still ramping, and grab an
      unfair head start.
    - Each app is classified against its own current share: `hungry`
      (speed >= 90% of its share -- wants more) or `slack` (speed < 85% --
      demonstrably not using what it already has). The gap between the two
      thresholds is deliberate slack so a share doesn't flap back and forth
      right at the boundary.
    - One slack, other hungry: shrink the slack app's share down toward its
      actual speed plus a fixed fraction of `total` as headroom (room to
      grow back later if its demand increases again), and hand the
      difference to the hungry side.
    - Both hungry (both genuinely want more than they have): nudge both a
      bounded step toward the midpoint of their current shares, converging
      toward an even split over several cycles rather than letting one
      side run away -- unlike inflating a demand *estimate* every cycle a
      side stays pinned near its cap (which can't tell "still growing
      toward true capacity" from "already has way more than a fair share
      and is simply using all of it comfortably"), this only ever moves
      share toward parity, so it can't run away indefinitely.
    - Neither hungry nor slack (both comfortably mid-range): leave shares
      unchanged -- doesn't affect either app's real throughput either way,
      since neither is actually constrained by its own cap in this regime.
    - Every branch decides a single new qBittorrent share, clamped to
      `[total * MIN_SHARE_FRACTION, total * (1 - MIN_SHARE_FRACTION)]`;
      SABnzbd's share is always `total - qbit's share`, so the two can
      never sum to more than `total` -- guaranteed by construction, not by
      a best-effort correction step afterward, and never negative
      regardless of how small `total` is (there's no separate absolute
      floor value left to conflict with it).
    """
    qbit_active = qbit_speed > active_threshold
    sab_active = sab_speed > active_threshold

    if not (qbit_active and sab_active):
        return round(total), round(total)

    if qbit_limit >= total and sab_limit >= total:
        qbit_limit = sab_limit = total / 2

    qbit_hungry = qbit_limit > 0 and qbit_speed >= qbit_limit * 0.9
    sab_hungry = sab_limit > 0 and sab_speed >= sab_limit * 0.9
    qbit_slack = qbit_limit > 0 and qbit_speed < qbit_limit * 0.85
    sab_slack = sab_limit > 0 and sab_speed < sab_limit * 0.85

    headroom = total * MIN_SHARE_FRACTION

    def _shrunk(limit: float, speed: float) -> float:
        """The slack app's new share: its demonstrated speed plus headroom
        to grow back later, never more than what it already has."""
        return min(limit, speed + headroom)

    new_qbit = qbit_limit

    if qbit_slack and sab_hungry:
        new_qbit = _shrunk(qbit_limit, qbit_speed)
    elif sab_slack and qbit_hungry:
        new_qbit = total - _shrunk(sab_limit, sab_speed)
    elif qbit_hungry and sab_hungry:
        step = total * 0.05
        midpoint = (qbit_limit + sab_limit) / 2
        if qbit_limit < midpoint:
            new_qbit = min(qbit_limit + step, midpoint)
        elif sab_limit < midpoint:
            new_qbit = max(qbit_limit - step, midpoint)

    new_qbit = max(headroom, min(total - headroom, new_qbit))
    new_sab = total - new_qbit

    return round(new_qbit), round(new_sab)


class OvershootCompensator:
    """Tracks whether combined actual download speed keeps exceeding the
    effective budget despite allocate()'s computed split, and if so grows a
    corrective reduction to apply to qBittorrent's limit specifically --
    UDP-heavy torrent traffic (many peer connections, uTP's own per-peer
    congestion control) is inherently harder for an app to rate-limit
    precisely than SABnzbd's more predictable usenet transfers, so
    qBittorrent is the one that typically doesn't respect its assigned cap
    exactly under load.

    The correction accumulates every cycle overshoot is confirmed (rather
    than being recomputed from scratch) so it always eventually clears a
    caller's hysteresis gate, and decays gradually once actual usage is
    back within budget so a past correction doesn't linger once it's no
    longer needed.

    Growth is capped at a small fraction of the budget per cycle: what's
    being tracked is the *persistent, steady-state* correction size, and a
    one-off transient excursion must not be allowed to inflate the penalty
    toward the full budget. Without the cap, a transient overshoot many
    times the size of the budget (e.g. both apps' speeds still ramping
    down from primary-link levels against a backup budget that just became
    dozens of times smaller) maxes the penalty out, and the slow decay then
    leaves qBittorrent pinned at its floor for minutes over a discrepancy
    that self-corrected within seconds. The cap doesn't delay a genuine
    correction: applies are paced by the caller's overshoot settle gate
    anyway, and several cycles of growth at this rate always reach a
    realistic correction size (qBittorrent's rate-limiting imprecision is
    a few percent of the budget, not multiples of it) before the gate next
    allows one.
    """

    MARGIN_FRACTION = 0.03
    CONFIRM_CYCLES = 2
    DECAY_FRACTION = 0.01
    GROWTH_FRACTION = 0.02

    def __init__(self):
        self.penalty = 0.0
        self._over_count = 0

    def update(self, combined_speed: float, effective_total: float) -> float:
        """Call once per poll cycle with this cycle's actual combined speed
        and the currently-effective budget. Returns the current penalty
        (bytes/sec) to subtract from qBittorrent's computed limit."""
        margin = effective_total * self.MARGIN_FRACTION
        overshoot = combined_speed - effective_total

        if overshoot > margin:
            self._over_count += 1
            if self._over_count >= self.CONFIRM_CYCLES:
                increment = min(overshoot, effective_total * self.GROWTH_FRACTION)
                self.penalty = min(effective_total, self.penalty + increment)
        else:
            self._over_count = 0
            self.penalty = max(0.0, self.penalty - effective_total * self.DECAY_FRACTION)

        return self.penalty


class Arbitrator:
    """Bundles allocate() + OvershootCompensator + the settle-timer gate
    into the one per-cycle arbitration decision main.py's poll loop needs,
    so it can be driven directly by a test (including realistic,
    fluctuating simulations) without faking any I/O.

    qbit_limit/sab_limit are the values actually applied via each app's
    API -- set them directly after a successful set_download_limit() call;
    step() never touches them itself, only recommends new values, so a
    failed API call correctly leaves the tracked "currently applied"
    value unchanged. qbit_fair_share is allocate()'s own bookkeeping and
    IS updated internally by step(). It is never adjusted by the overshoot
    penalty, so fairness never operates on penalty-adjusted share numbers
    -- but it's not a perfect isolation: allocate() classifies each app
    against its own current share, and a qbit pinned at its (penalty-
    reduced) applied limit reports a speed that can read as "slack"
    against the un-penalized fair share, compounding the shrink by up to
    one headroom step. That is bounded and self-correcting once the
    penalty decays (sim-verified by
    test_persistent_overshoot_stays_bounded_and_sab_does_not_wander) -- a
    deliberate trade against letting the penalty feed back into fairness
    bookkeeping directly.
    """

    def __init__(self, total: float):
        self.qbit_fair_share = total
        # Assumed, not known: whatever the apps really have is overwritten
        # on the first cycle regardless (see step()).
        self.qbit_limit = total
        self.sab_limit = total
        self.last_reallocation_at = 0.0
        self.last_qbit_apply_at = 0.0
        self.overshoot_compensator = OvershootCompensator()
        self._last_total = total
        self._first_cycle = True

    def step(
        self,
        now: float,
        qbit_speed: float,
        sab_speed: float,
        total: float,
        active_threshold: float,
        reallocation_settle_seconds: float,
        rebaseline: bool,
        overshoot_settle_seconds: float = 15.0,
    ) -> tuple[float, bool, float, bool, float]:
        """Call once per poll cycle. Returns (new_qbit_limit,
        qbit_should_apply, new_sab_limit, sab_should_apply,
        overshoot_penalty). `rebaseline` says the current shares are
        stale -- a WAN budget swap, or an app returning from a confirmed
        outage during which the other held the whole budget -- and both
        apps should restart from a neutral half/half baseline."""
        first_cycle, self._first_cycle = self._first_cycle, False

        # SAB has no separate fair-share bookkeeping: its share IS its
        # applied limit (always total minus qbit's share). A rebaseline
        # therefore feeds allocate() a neutral half as SAB's share without
        # writing it to self.sab_limit -- that stays what's really applied
        # until the caller's (forced, see below) apply succeeds, which is
        # what keeps it truthful if that apply fails or SAB is unreachable.
        sab_share = total / 2 if rebaseline else self.sab_limit
        if rebaseline:
            # A WAN failover budget swap invalidates any existing share as
            # a fraction of the OLD total -- reset to a neutral baseline on
            # the new one, same as allocate()'s own first-both-active
            # reset. Without this, allocate()'s defensive floor/ceiling
            # clamp (relative to the NEW total) can force a spurious jump
            # completely disconnected from actual demand, e.g. recovering
            # from a converged 25/25 split on a 50 Mbps backup link to an
            # 800 Mbps primary would otherwise force qbit up to 80 Mbps
            # immediately (10% of the new total), purely because 25 fell
            # below that floor -- not because of any fairness decision.
            # A peer returning from a confirmed outage is the same
            # situation from the other side: the survivor's fair share
            # drifted to the whole budget while it ran alone, and resuming
            # from 100/0 would clamp the returning app to the floor.
            self.qbit_fair_share = total / 2

        if total != self._last_total:
            # The overshoot penalty is sized against the total it was built
            # up under, so against a much smaller new budget even a modest
            # correction immediately slams qBittorrent's limit to the
            # floor, where it lingers for as long as the slow decay takes
            # against the new total -- and being floor-pinned also makes
            # qbit read as "slack" to allocate(), handing its share away.
            # Any overshoot genuinely ongoing against the new budget
            # re-confirms and re-grows a correctly-sized penalty within a
            # few cycles, so resetting is safe in the conservative
            # direction. Keyed on the total itself rather than on
            # `rebaseline`: a rebaseline for a peer returning from an
            # outage leaves the budget unchanged, and a still-valid
            # penalty should survive it.
            self.overshoot_compensator = OvershootCompensator()
            self._last_total = total

        new_qbit_fair_share, new_sab_limit = allocate(
            qbit_speed, sab_speed, self.qbit_fair_share, sab_share, total, active_threshold,
        )
        fairness_allowed = (
            first_cycle or rebaseline
            or now - self.last_reallocation_at >= reallocation_settle_seconds
        )
        fairness_changed = new_qbit_fair_share != self.qbit_fair_share or new_sab_limit != self.sab_limit

        overshoot_penalty = self.overshoot_compensator.update(qbit_speed + sab_speed, total)
        new_qbit_limit = new_qbit_fair_share
        if overshoot_penalty > 0:
            new_qbit_limit = max(total * MIN_SHARE_FRACTION, new_qbit_fair_share - overshoot_penalty)

        # Overshoot correction reacts faster than fairness reallocation (it's
        # a budget-safety mechanism, not a fairness one), but still needs
        # its own settle gate -- otherwise a penalty that's merely decaying
        # by a fixed fraction each cycle (or growing by a slightly different
        # amount each cycle) forces a new limit to be pushed to qBittorrent
        # on literally every poll, never giving its own rate limiter a
        # stable target to actually settle into.
        overshoot_apply = overshoot_penalty > 0 and new_qbit_limit != self.qbit_limit and (
            first_cycle or rebaseline or now - self.last_qbit_apply_at >= overshoot_settle_seconds
        )

        # A rebaseline bypasses the settle gate (via fairness_allowed) but is
        # otherwise an ordinary decision: nothing is re-applied when the new
        # value already matches what the app has -- a no-op set would only
        # restart the overshoot settle window for nothing. The first cycle
        # is the exception: the tracked values start as an assumption (see
        # __init__), and whatever stale/manual limit an app actually has
        # must be overwritten even if the decision happens to be "full
        # budget" -- which it always is while at most one app is active.
        qbit_should_apply = first_cycle or (fairness_allowed and new_qbit_limit != self.qbit_limit) or overshoot_apply
        sab_should_apply = first_cycle or (fairness_allowed and new_sab_limit != self.sab_limit)

        if qbit_should_apply:
            self.last_qbit_apply_at = now

        if fairness_changed and fairness_allowed:
            self.qbit_fair_share = new_qbit_fair_share
            self.last_reallocation_at = now

        return new_qbit_limit, qbit_should_apply, new_sab_limit, sab_should_apply, overshoot_penalty
