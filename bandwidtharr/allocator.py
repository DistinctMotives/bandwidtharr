"""Pure bandwidth-allocation logic, kept free of I/O so it's cheap to unit test."""


def allocate(
    qbit_speed: float,
    sab_speed: float,
    qbit_limit: float,
    sab_limit: float,
    total: float,
    min_floor: float,
    active_threshold: float,
    probe_step: float,
) -> tuple[int, int]:
    """Return (qbit_limit, sab_limit) in the same unit as the inputs (bytes/sec).

    - Whenever at most one app is active, both get the full budget as a ceiling --
      a lone downloader gets all of it, and an idle app stays uncapped so it can
      ramp immediately if it starts (at the cost of a brief, one-poll-interval
      overshoot above budget right when the second app kicks in).
    - Only once both are simultaneously active does the split kick in, proportional
      to demand rather than a flat 50/50, so whichever app can actually use more
      bandwidth gets more of it. `min_floor` only matters here, to stop either side
      being squeezed to zero by the other's demand.
    - "Demand" for an app pinned at its own limit is estimated as limit + probe_step
      rather than its measured speed, since a saturated app's measured speed just
      reflects the cap we gave it last round, not what it actually wants.
    """
    qbit_active = qbit_speed > active_threshold
    sab_active = sab_speed > active_threshold

    if not (qbit_active and sab_active):
        return round(total), round(total)

    qbit_demand = qbit_limit + probe_step if qbit_limit > 0 and qbit_speed >= qbit_limit * 0.9 else qbit_speed
    sab_demand = sab_limit + probe_step if sab_limit > 0 and sab_speed >= sab_limit * 0.9 else sab_speed

    combined = qbit_demand + sab_demand
    qbit_share = qbit_demand / combined if combined > 0 else 0.5

    new_qbit = max(min_floor, round(total * qbit_share))
    new_sab = max(min_floor, round(total * (1 - qbit_share)))

    overflow = (new_qbit + new_sab) - total
    if overflow > 0:
        if new_qbit >= new_sab:
            new_qbit -= overflow
        else:
            new_sab -= overflow

    return round(new_qbit), round(new_sab)
