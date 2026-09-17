"""Bounded paragraph leading adjustment; unresolved contraction blocks delivery."""
from statistics import median


def plan_vertical_spacing(source_baselines, target_baselines, font_size, *, fixed_inline=False):
    source = sorted(set(source_baselines))
    target = []
    # Native assets move on integer coordinates, within one point of Chinese baselines.
    for baseline in sorted(target_baselines):
        if not target or baseline - target[-1] > 1.01:
            target.append(baseline)
    result = {"shifts": [(b, 0) for b in target], "adjusted": False,
              "unresolved": False, "reason": "no_contraction", "residual_points": 0.0}
    if fixed_inline or len(source) < 2:
        return result
    leading = median(b-a for a,b in zip(source, source[1:]))
    source_span = source[-1]-source[0]
    target_span = target[-1]-target[0] if target else 0
    shortage = max(0.0, source_span-target_span)
    result["residual_points"] = shortage
    if shortage < leading * .75:
        return result
    # Do not fake a missing line by making one huge gap, scaling text, or padding prose.
    if len(target) < 2:
        result.update(unresolved=True, reason="single_line_or_empty_contraction")
        return result
    offsets = [round(shortage*i/(len(target)-1)) for i in range(len(target))]
    moved = [b+offset for b,offset in zip(target,offsets)]
    if any(b-a > font_size*1.8+.01 for a,b in zip(moved,moved[1:])):
        result.update(unresolved=True, reason="required_leading_exceeds_limit")
        return result
    result.update(shifts=list(zip(target,offsets)), adjusted=True,
                  reason="bounded_leading", residual_points=max(0.,source_span-(moved[-1]-moved[0])))
    return result


def baseline_shift(baseline, shifts):
    if not shifts:
        return 0
    nearest = min(shifts, key=lambda pair: abs(pair[0]-baseline))
    if abs(nearest[0]-baseline) > 1.01:
        raise ValueError("placement has no declared translated baseline")
    return nearest[1]
