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
) -> tuple[int, int, bool]:
    """Return (qbit_limit, sab_limit, saturating) in the same unit as the
    inputs (bytes/sec), where `saturating` is True when either app is
    currently pinned at (>= 90% of) its own current share while both are
    active.

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
    - `saturating` tells the caller this update reflects that genuine
      demand change, not measurement noise -- callers gating applied
      changes behind a hysteresis/change-threshold should bypass it
      whenever `saturating` is True. Skipping that would risk a deadlock:
      if a correction this small never clears the threshold, the limits
      never change, so next cycle's inputs are identical and produce the
      identical too-small correction again, forever.
    """
    qbit_active = qbit_speed > active_threshold
    sab_active = sab_speed > active_threshold

    if not (qbit_active and sab_active):
        return round(total), round(total), False

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

    return round(new_qbit), round(new_sab), qbit_hungry or sab_hungry


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
    """

    MARGIN_FRACTION = 0.03
    CONFIRM_CYCLES = 2
    DECAY_FRACTION = 0.01

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
                self.penalty = min(effective_total, self.penalty + overshoot)
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
    value unchanged. qbit_fair_share is allocate()'s own bookkeeping
    (untouched by the overshoot penalty, so the penalty never distorts
    next cycle's fairness classification or midpoint calculation) and IS
    updated internally by step().
    """

    def __init__(self, total: float):
        self.qbit_fair_share = total
        self.qbit_limit = total
        self.sab_limit = total
        self.last_reallocation_at = 0.0
        self.overshoot_compensator = OvershootCompensator()
        self._first_cycle = True

    def step(
        self,
        now: float,
        qbit_speed: float,
        sab_speed: float,
        total: float,
        active_threshold: float,
        reallocation_settle_seconds: float,
        link_changed: bool,
    ) -> tuple[float, bool, float, bool, float]:
        """Call once per poll cycle. Returns (new_qbit_limit,
        qbit_should_apply, new_sab_limit, sab_should_apply,
        overshoot_penalty)."""
        first_cycle, self._first_cycle = self._first_cycle, False
        previous_sab_limit = self.sab_limit  # what's actually applied right now, before any reset below

        if link_changed:
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
            self.qbit_fair_share = self.sab_limit = total / 2

        new_qbit_fair_share, new_sab_limit, saturating = allocate(
            qbit_speed, sab_speed, self.qbit_fair_share, self.sab_limit, total, active_threshold,
        )
        fairness_allowed = (
            first_cycle or link_changed
            or now - self.last_reallocation_at >= reallocation_settle_seconds
        )
        fairness_changed = new_qbit_fair_share != self.qbit_fair_share or new_sab_limit != self.sab_limit

        overshoot_penalty = self.overshoot_compensator.update(qbit_speed + sab_speed, total)
        new_qbit_limit = new_qbit_fair_share
        if overshoot_penalty > 0:
            new_qbit_limit = max(total * MIN_SHARE_FRACTION, new_qbit_fair_share - overshoot_penalty)

        # link_changed always applies fresh values to both sides -- the old
        # applied values are stale/meaningless against the new total
        # regardless of whether either happens to numerically match (the
        # sab_limit reset above would otherwise make that comparison miss a
        # coincidental match against its own just-reset value).
        qbit_should_apply = link_changed or overshoot_penalty > 0 or (fairness_allowed and new_qbit_limit != self.qbit_limit)
        sab_should_apply = link_changed or (fairness_allowed and new_sab_limit != previous_sab_limit)

        if fairness_changed and fairness_allowed:
            self.qbit_fair_share = new_qbit_fair_share
            self.last_reallocation_at = now

        return new_qbit_limit, qbit_should_apply, new_sab_limit, sab_should_apply, overshoot_penalty
