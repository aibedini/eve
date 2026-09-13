"""Package selection: the recommended offer and a peace-of-mind alternative.

RFP sections 21-23 and tests 45.10-45.12, 45.17, 45.18. Pure functions over the catalog and
the forecast:

* the **recommended** package is the smallest finite offer that safely covers the forecast
  *for its own duration* (a 7-day package is measured against 7 days, not a month) - the
  safety margin buys headroom on the primary recommendation, not only on the comfort one;
* the **comfort** package is one meaningful level up, so "what if I use more" has an answer
  without guessing;
* **unlimited** is never chosen while a finite package covers the demand, and when it is
  chosen because nothing finite can, the caller can say so;
* when even the largest finite package cannot cover the demand the choice is flagged
  ``capacity_limited`` with its shortfall instead of pretending the plan is enough.
"""
from panel.services.usage_intelligence.schemas import (
    ROLLING_WINDOW_DAYS,
    PackageChoice,
)

COMFORT_STEP = 1.25
FIT_TOLERANCE_GB = 0.01


def _normalise(packages):
    rows = []
    for package in packages or []:
        try:
            days = int(package.get('days') or 0)
            volume = int(package.get('volume') or 0)
            price = max(0, int(package.get('price') or 0))
            package_id = package.get('id')
        except (AttributeError, TypeError, ValueError):
            continue
        if package_id is None:
            continue
        rows.append({
            'id': int(package_id),
            'name': str(package.get('name') or ''),
            'days': days,
            'volume': volume,
            'price': price,
            'unlimited': volume == 0,
        })
    return rows


def _horizon(package):
    return int(package['days']) if package['days'] > 0 else ROLLING_WINDOW_DAYS


def required_for(package, daily_gb, *, safety_margin=0.0):
    """The volume this package must cover for its own duration."""
    horizon = _horizon(package)
    return max(0.0, float(daily_gb)) * horizon * (1.0 + float(safety_margin))


def _choice(package, *, required_gb, safety_margin, capacity_limited=False,
            reason=''):
    volume = int(package['volume'] or 0)
    shortfall = 0.0
    if capacity_limited and volume > 0:
        shortfall = max(0.0, required_gb - volume)
    return PackageChoice(
        package_id=int(package['id']),
        package_name=str(package['name']),
        package_volume_gb=volume,
        package_days=int(package['days'] or 0),
        package_price=int(package['price'] or 0),
        capacity_limited=bool(capacity_limited),
        capacity_shortfall_gb=round(shortfall, 1),
        unlimited=bool(package['unlimited']),
        required_gb=round(float(required_gb), 1),
        reason=reason,
    )


def _finite(packages):
    return [package for package in packages if not package['unlimited']]


def select_packages(packages, forecast, *, safety_margin=None) -> dict:
    """Return {'recommended': PackageChoice|None, 'comfort': PackageChoice|None}."""
    rows = _normalise(packages)
    if not rows:
        return {'recommended': None, 'comfort': None}

    rate = max(0.0, float(getattr(forecast, 'average_daily_gb', 0.0) or 0.0))
    margin = (float(getattr(forecast, 'safety_margin_percent', 0) or 0) / 100.0
              if safety_margin is None else float(safety_margin))
    finite = sorted(_finite(rows), key=lambda row: (row['volume'], row['price'], row['id']))
    unlimited = sorted((row for row in rows if row['unlimited']),
                       key=lambda row: (row['price'], row['id']))

    # No usage evidence: the safest answer is the smallest offer, never an upsell.
    if rate <= 0:
        lowest = finite[0] if finite else (unlimited[0] if unlimited else None)
        if lowest is None:
            return {'recommended': None, 'comfort': None}
        chosen = _choice(lowest, required_gb=0.0, safety_margin=margin,
                         reason='no_usage_lowest')
        return {'recommended': chosen, 'comfort': None}

    def fits(package, *, buffer_ratio=1.0):
        needed = required_for(package, rate, safety_margin=margin) * buffer_ratio
        if package['unlimited']:
            return True, needed
        return (package['volume'] + FIT_TOLERANCE_GB) >= needed, needed

    covering = []
    for package in finite:
        ok, needed = fits(package)
        if ok:
            covering.append((package, needed))
    covering.sort(key=lambda item: (item[0]['volume'], item[0]['price'], item[0]['id']))

    if covering:
        package, needed = covering[0]
        recommended = _choice(package, required_gb=needed, safety_margin=margin,
                              reason='covers_forecast')
    elif unlimited:
        # Only when nothing finite can cover the demand (RFP section 23).
        package = unlimited[0]
        _, needed = fits(package)
        recommended = _choice(package, required_gb=needed, safety_margin=margin,
                              reason='no_finite_package_covers')
    else:
        # The largest finite offer is the best available; say it is not enough.
        package = finite[-1]
        _, needed = fits(package)
        recommended = _choice(package, required_gb=needed, safety_margin=margin,
                              capacity_limited=True, reason='largest_package_insufficient')

    # Comfort: one meaningful level above the recommendation.
    comfort = None
    if not recommended.unlimited and finite:
        target_volume = max(
            recommended.package_volume_gb * COMFORT_STEP,
            recommended.package_volume_gb + FIT_TOLERANCE_GB,
        )
        candidates = [package for package in finite
                      if package['volume'] > recommended.package_volume_gb]
        if candidates:
            bigger = min(candidates, key=lambda row: (row['volume'], row['price'], row['id']))
            if bigger['volume'] < target_volume:
                # Nothing at 1.25x: offer the next real step, which is what the catalog has.
                bigger = min(candidates, key=lambda row: (row['volume'], row['price'], row['id']))
            _, needed = fits(bigger)
            comfort = _choice(bigger, required_gb=needed, safety_margin=margin,
                              reason='comfort_step_up')
        elif unlimited:
            _, needed = fits(unlimited[0])
            comfort = _choice(unlimited[0], required_gb=needed, safety_margin=margin,
                              reason='comfort_unlimited')
    return {'recommended': recommended, 'comfort': comfort}
