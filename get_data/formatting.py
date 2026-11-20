"""Number formatting shared by the reports (analyse, dashboard).

dashboard/template.html carries a JavaScript copy of percent() for the chart page;
keep the two in step.
"""

from __future__ import annotations


def percent(part, whole) -> str:
    """Share as text that never rounds a partial share to 100 % or a non-zero one to 0 %.

    148,842 of 148,909 is 99.955 %, which plain rounding shows as 100 %; here it is
    99.9 %, so the 67 games that are missing stay visible.
    """
    if not whole:
        return "-"
    if part == whole:
        return "100 %"
    if not part:
        return "0 %"
    return f"{min(max(100 * part / whole, 0.1), 99.9):.1f} %"
