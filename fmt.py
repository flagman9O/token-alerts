"""Number formatting shared by the alert cards and the settings menu."""


def money(v):
    if v is None:
        return "—"
    v = float(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def hours(v):
    v = float(v or 0)
    if v <= 0:
        return "без ограничения"
    if v < 24:
        return f"{v:.0f} ч"
    return f"{v/24:.0f} дн"
