import decimal


def sec2time(sec: decimal.Decimal | int | float) -> str:
    """
    Convert a number of seconds into a human-readable string (e.g. "2h 5min 30sec").
    Rounds to whole seconds before splitting to avoid rollover like "59min 60sec".
    """
    total_seconds = int(decimal.Decimal(str(sec)).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}min {seconds}sec" if hours > 0 else f"{minutes}min {seconds}sec"


__all__ = ["sec2time"]
