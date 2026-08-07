def clamp_score(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, int(value)))


def normalize_risk(score):
    score = clamp_score(score)

    if score >= 85:
        return "high"

    if score >= 65:
        return "medium"

    return "low"
