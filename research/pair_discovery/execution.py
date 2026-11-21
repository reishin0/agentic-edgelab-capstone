"""Two-leg, development-only execution replay for the equity discovery screen.

Times in the input and ledger are milliseconds since midnight New York time.
Amounts are USD and sizes are shares. Rates are annual basis points except
sell_fee_bps, which is basis points per transaction. A LegDay contains conditions
effective before the opening window and ordered intraday quote/borrow events.
No history is loaded here. The caller supplies a complete exchange calendar and
the spread and stability evidence derived from the frozen development protocol.
"""

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import floor, isfinite
from statistics import median


CALIBRATION_START = date(2025, 1, 2)
CALIBRATION_END = date(2025, 10, 31)
OPEN_MS = (9 * 60 + 35) * 60_000
WINDOW_END_MS = (9 * 60 + 40) * 60_000
MAX_QUOTE_AGE_MS = 1_000
LEG_DELAY_MS = 60_000
CAPITAL = 25_000.0
MAX_HOLD = 20
REGULAR_OPEN_MS = (9 * 60 + 30) * 60_000
VOLUME_LOOKBACK = 20


def require_number(value, label, *, minimum=0, strict=False):
    if (type(value) not in (float, int) or not isfinite(value)
            or (value <= minimum if strict else value < minimum)):
        raise ValueError(f"Invalid {label}")


def require_clock(value):
    if type(value) is not int or not 0 <= value < 86_400_000:
        raise ValueError("Invalid intraday event time")


@dataclass(frozen=True, slots=True)
class Quote:
    at_ms: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    eligible: bool = True

    def __post_init__(self):
        require_clock(self.at_ms)
        require_number(self.bid, "quote bid", strict=True)
        require_number(self.ask, "quote ask", strict=True)
        if (type(self.bid_size) is not int or self.bid_size < 0 or type(self.ask_size) is not int
                or self.ask_size < 0 or type(self.eligible) is not bool):
            raise ValueError("Invalid quote sizes or eligibility")

    @property
    def midpoint(self):
        return (self.bid + self.ask) / 2

    def usable(self, at_ms):
        return self.eligible and self.bid <= self.ask and 0 <= at_ms - self.at_ms <= MAX_QUOTE_AGE_MS


@dataclass(frozen=True, slots=True)
class Borrow:
    at_ms: int
    available_shares: int
    rate_bps: float | None
    restricted: bool = False
    recalled: bool = False

    def __post_init__(self):
        require_clock(self.at_ms)
        if type(self.available_shares) is not int or self.available_shares < 0:
            raise ValueError("Invalid borrow availability")
        if type(self.restricted) is not bool or type(self.recalled) is not bool:
            raise ValueError("Invalid borrow restriction or recall")
        if self.rate_bps is not None:
            require_number(self.rate_bps, "borrow rate")


def check_events(events, kind):
    if type(events) is not tuple or any(not isinstance(event, kind) for event in events):
        raise ValueError(f"Expected a tuple of {kind.__name__} events")
    timestamps = tuple(event.at_ms for event in events)
    if timestamps != tuple(sorted(set(timestamps))):
        raise ValueError(f"{kind.__name__} times must be strictly increasing")


@dataclass(frozen=True, slots=True)
class LegDay:
    quotes: tuple[Quote, ...]
    mark: Quote | None
    median_volume: float | None
    borrow: tuple[Borrow, ...]
    financing_bps: float | None
    commission_per_share: float | None
    sell_fee_bps: float | None
    halted: bool = False
    split_ratio: float = 1.0
    cash_in_lieu: float | None = None
    dividend: float = 0.0
    settlement: float | None = None

    def __post_init__(self):
        check_events(self.quotes, Quote)
        check_events(self.borrow, Borrow)
        require_number(self.split_ratio, "split ratio", strict=True)
        require_number(self.dividend, "dividend")
        if ((self.mark is not None and not isinstance(self.mark, Quote)) or type(self.halted) is not bool):
            raise ValueError("Invalid close mark or halt state")
        for name in ("median_volume", "financing_bps", "commission_per_share", "sell_fee_bps",
                     "cash_in_lieu", "settlement"):
            if (value := getattr(self, name)) is not None:
                require_number(value, name)


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    z: float
    stable: bool
    legs: tuple[LegDay, LegDay]
    close_ms: int = 57_600_000

    def __post_init__(self):
        if type(self.day) is not date or type(self.z) not in (float, int) or not isfinite(self.z):
            raise ValueError("Invalid session date or close z-score")
        if type(self.stable) is not bool or type(self.legs) is not tuple or len(self.legs) != 2:
            raise ValueError("Expected two ordered leg records and a stability verdict")
        require_clock(self.close_ms)
        if self.close_ms <= WINDOW_END_MS:
            raise ValueError("Session closes before the execution window ends")
        for leg in self.legs:
            if not isinstance(leg, LegDay):
                raise ValueError("Missing leg session record")
            if ((leg.quotes and leg.quotes[-1].at_ms > self.close_ms)
                    or (leg.borrow and leg.borrow[-1].at_ms > self.close_ms)
                    or (leg.mark is not None and leg.mark.at_ms > self.close_ms)):
                raise ValueError("Events after the session close are ineligible")


def decode_leg(record: Mapping) -> LegDay:
    """Convert one sanitized, source-bound instrument/session JSON object."""
    if not isinstance(record, Mapping):
        raise ValueError("Expected an instrument/session execution record")
    try:
        fields = dict(record)
        if fields["mark"] is not None:
            fields["mark"] = Quote(**fields["mark"])
        fields["quotes"] = tuple(Quote(**quote) for quote in fields["quotes"])
        fields["borrow"] = tuple(Borrow(**entry) for entry in fields["borrow"])
        return LegDay(**fields)
    except (TypeError, KeyError) as error:
        raise ValueError("Invalid instrument/session execution record fields") from error


def reconciled_legs(key, records, bars, seed_days, sessions):
    """Validate execution against signal actions and exclusively prior raw share volume."""
    if len(seed_days) < VOLUME_LOOKBACK:
        raise ValueError(f"Insufficient trailing consolidated volume history for {key}")
    volumes = [bars[stamp]["volume"] for stamp in seed_days[-VOLUME_LOOKBACK:]]
    for stamp in sessions:
        leg = decode_leg(records[stamp])
        signal = bars[stamp]
        if leg.split_ratio != signal["split"] or leg.dividend != signal["distribution"]:
            raise ValueError(f"Signal and execution corporate actions differ on {stamp} for {key}")
        if leg.median_volume != median(volumes[-VOLUME_LOOKBACK:]):
            raise ValueError(f"Trailing consolidated volume differs on {stamp} for {key}")
        yield leg
        volumes.append(signal["volume"])


def latest(events, at_ms):
    index = bisect_right(events, at_ms, key=lambda event: event.at_ms) - 1
    return events[index] if index >= 0 else None


def quote_at(leg, at_ms):
    if leg.halted or leg.settlement is not None:
        return None
    quote = latest(leg.quotes, at_ms)
    return quote if quote is not None and quote.usable(at_ms) else None


def trade_times(legs, start, end):
    if start > end:
        return ()
    return sorted({start, *(quote.at_ms for leg in legs for quote in leg.quotes
                            if start < quote.at_ms <= end)})


def synchronized(legs, start=OPEN_MS, end=WINDOW_END_MS):
    """Return the first usable simultaneous quote pair within the open window."""
    for at_ms in trade_times(legs, start, end):
        first, second = (quote_at(leg, at_ms) for leg in legs)
        if first is not None and second is not None:
            return at_ms, (first, second)
    return None


def execution_price(quote, side, stress):
    half_spread = (quote.ask - quote.bid) / 2
    before_slippage = quote.midpoint + side * half_spread * (2 if stress else 1)
    return before_slippage + side * before_slippage * (0.001 if stress else 0.0005)


def prices_and_weights(quotes, entry_side, beta, stress):
    weights = 1 / (1 + beta), beta / (1 + beta)
    sides = -entry_side, entry_side
    prices = tuple(execution_price(quote, side, stress) for quote, side in zip(quotes, sides, strict=True))
    if min(prices) <= 0:
        return None
    quantities = tuple(floor(weight * CAPITAL / price) for weight, price in zip(weights, prices, strict=True))
    if min(quantities) <= 0:
        return None
    notionals = tuple(qty * price for qty, price in zip(quantities, prices, strict=True))
    gross = sum(notionals)
    if any(abs(amount / gross - target) > .05
           for amount, target in zip(notionals, weights, strict=True)):
        return None
    return tuple(side * qty for side, qty in zip(sides, quantities, strict=True))


def fill_issue(leg, quote, quantity, at_ms, stress, *, opening):
    if quote is None:
        return "missing_fresh_quote"
    if leg.median_volume is None:
        return "missing_volume"
    if any(rate is None for rate in (leg.commission_per_share, leg.sell_fee_bps)):
        return "missing_fill_fee"
    if opening and quantity > 0 and leg.financing_bps is None:
        return "missing_financing_rate"
    if opening and quantity < 0:
        borrow = latest(leg.borrow, at_ms)
        if borrow is None or borrow.rate_bps is None:
            return "missing_locate_or_borrow_rate"
        if borrow.restricted or borrow.recalled or borrow.available_shares < -quantity:
            return "insufficient_or_restricted_borrow"
    side = 1 if quantity > 0 else -1
    displayed = quote.ask_size if side > 0 else quote.bid_size
    if abs(quantity) > min(displayed * (.125 if stress else .25), leg.median_volume * .001):
        return "insufficient_capacity"
    if execution_price(quote, side, stress) <= 0:
        return "invalid_execution_price"
    return None


class Replay:
    def __init__(self, beta, stress):
        self.beta = beta
        self.stress = stress
        self.cash = self.gross_cash = CAPITAL
        self.quantities = [0, 0]
        self.ledger = []
        self.equity = []
        self.costs = dict(spread=0.0, slippage=0.0, fees=0.0, borrow=0.0, financing=0.0)
        self.reasons = Counter()
        self.missing = set()
        self.entry_attempts = self.entered = self.round_trips = 0
        self.quote_days = self.turnover = 0
        self.opened = self.entry_side = self.pending_exit = None
        self.pending_entry = self.unhedged_since = None
        self.open_times = [0, 0]
        self.last_fill = 0
        self.holding_sessions = []
        self.unhedged_intervals = []
        self.legging_intervals = []
        self.disabled = False

    def record(self, session, event, **details):
        self.ledger.append({"date": session.day.isoformat(), "event": event, **details})

    def missing_evidence(self, session, reason, **details):
        self.missing.add(reason)
        self.record(session, "missing_evidence", reason=reason, **details)

    def skip(self, session, reason, **details):
        self.reasons[reason] += 1
        self.record(session, "skip", reason=reason, **details)

    def fill(self, session, leg_index, quote, quantity, at_ms, *, opening):
        side = 1 if quantity > 0 else -1
        price = execution_price(quote, side, self.stress)
        leg = session.legs[leg_index]
        notional = abs(quantity) * price
        fee = abs(quantity) * leg.commission_per_share + (notional * leg.sell_fee_bps / 10_000 if side < 0 else 0)
        spread = abs(quantity) * (quote.ask - quote.bid) / 2 * (2 if self.stress else 1)
        slippage = quantity * (price - quote.midpoint) - spread
        self.cash -= quantity * price + fee
        self.gross_cash -= quantity * quote.midpoint
        self.costs["fees"] += fee
        self.costs["spread"] += spread
        self.costs["slippage"] += slippage
        self.turnover += notional
        self.quantities[leg_index] += quantity
        self.last_fill = max(self.last_fill, at_ms)
        if opening:
            self.open_times[leg_index] = at_ms
        self.record(session, "fill", leg=leg_index, at_ms=at_ms, quote_ms=quote.at_ms, signed_shares=quantity,
                    price=price, quote_mid=quote.midpoint, spread=spread, slippage=slippage, fee=fee, opening=opening)

    def charge_holding_costs(self, session, previous):
        if previous is None:
            return
        days = (session.day - previous.day).days
        for index, (quantity, leg) in enumerate(zip(self.quantities, previous.legs, strict=True)):
            if not quantity:
                continue
            mark = leg.mark
            if mark is None or not mark.usable(previous.close_ms):
                self.missing_evidence(session, "missing_financing_mark", leg=index)
                continue
            borrow = latest(leg.borrow, previous.close_ms)
            rate_name = "borrow" if quantity < 0 else "financing"
            rate = (borrow.rate_bps if borrow is not None else None) if quantity < 0 else leg.financing_bps
            if rate is None:
                self.missing_evidence(session, f"missing_{rate_name}_rate", leg=index)
                continue
            factor = 2 if self.stress and quantity < 0 else 1
            cost = abs(quantity) * mark.midpoint * days / 365 * rate / 10_000 * factor
            self.cash -= cost
            self.costs[rate_name] += cost
            self.record(session, "holding_cost", leg=index, kind=rate_name, amount=cost, days=days,
                        reference_date=previous.day.isoformat(), rate_bps=rate * factor)

    def book_cash_action(self, session, kind, index, amount):
        self.cash += amount
        self.gross_cash += amount
        self.record(session, kind, leg=index, cash_flow=amount)

    def corporate_actions(self, session):
        for index, leg in enumerate(session.legs):
            quantity = self.quantities[index]
            if not quantity:
                continue
            if leg.split_ratio != 1:
                adjusted = quantity * leg.split_ratio
                # Cash in lieu applies to signed fractional shares, including short liabilities.
                whole = int(adjusted)
                fractional = adjusted - whole
                if abs(fractional) > 1e-9:
                    if leg.cash_in_lieu is None:
                        self.missing_evidence(session, "missing_cash_in_lieu", leg=index)
                    else:
                        self.book_cash_action(session, "cash_in_lieu", index, fractional * leg.cash_in_lieu)
                self.quantities[index] = quantity = whole
                self.record(session, "split", leg=index, ratio=leg.split_ratio, shares=quantity)
                if not quantity:
                    self.pending_exit = "split_cash_in_lieu"
            if leg.dividend:
                self.book_cash_action(session, "dividend", index, quantity * leg.dividend)
            if leg.settlement is not None:
                self.book_cash_action(session, "settlement", index, quantity * leg.settlement)
                self.quantities[index] = 0
                self.pending_exit = "cash_settlement"

    def find_fill(self, leg, quantity, start, end, *, opening=False):
        decisive_reason = "missing_fresh_quote"
        timestamps = trade_times((leg,), start, end)
        if opening and quantity < 0:
            timestamps = sorted(set(timestamps) | {event.at_ms for event in leg.borrow if start <= event.at_ms <= end})
        for at_ms in timestamps:
            quote = quote_at(leg, at_ms)
            reason = fill_issue(leg, quote, quantity, at_ms, self.stress, opening=opening)
            if reason is None:
                return at_ms, quote, None
            if decisive_reason == "missing_fresh_quote":
                decisive_reason = reason
        return None, None, decisive_reason

    def record_delay(self, day, phase, leg_index, at_ms, completed_at, quantity, reference, filled):
        side = 1 if quantity > 0 else -1
        record = {"date": day.day.isoformat(), "phase": phase, "exposed_leg": leg_index,
                  "duration_ms": completed_at - at_ms, "start_ms": at_ms, "end_ms": completed_at,
                  "legging_pnl_vs_reference_fill": quantity * (execution_price(reference, side, self.stress)
                                                               - execution_price(filled, side, self.stress))}
        self.legging_intervals.append(record)
        self.record(day, "legging_interval", **{key: value for key, value in record.items() if key != "date"})

    def open_pair(self, day, index, snapshot):
        entry_side, self.pending_entry = self.pending_entry, None
        self.entry_attempts += 1
        if snapshot is None:
            self.skip(day, "missing_synchronized_quote", phase="entry")
            return
        at_ms, quotes = snapshot
        signed = prices_and_weights(quotes, entry_side, self.beta, self.stress)
        if signed is None:
            self.skip(day, "invalid_rounded_weights", phase="entry")
            return
        for leg_index, (leg, quote, quantity) in enumerate(zip(day.legs, quotes, signed, strict=True)):
            if (reason := fill_issue(leg, quote, quantity, at_ms, self.stress, opening=True)) is not None:
                self.skip(day, reason, phase="entry", leg=leg_index, at_ms=at_ms)
                return

        self.fill(day, 0, quotes[0], signed[0], at_ms, opening=True)
        if self.stress:
            filled_at, second, reason = self.find_fill(day.legs[1], signed[1], at_ms + LEG_DELAY_MS,
                                                       WINDOW_END_MS, opening=True)
            recalled_at = self.recall_time(day, at_ms, filled_at if filled_at is not None else WINDOW_END_MS)
            if recalled_at is not None or reason is not None:
                reason = "borrow_recall" if recalled_at is not None else reason
                self.start_unhedged(day, at_ms, "entry", reason, leg_index=0)
                self.skip(day, reason, phase="delayed_entry", leg=1)
                self.pending_exit = "failed_second_entry_leg"
                start = recalled_at if recalled_at is not None else WINDOW_END_MS + 1
                self.close_inventory(day, index, start=start, end=day.close_ms)
                return
            self.record_delay(day, "entry", 0, at_ms, filled_at, signed[1], quotes[1], second)
            at_ms = filled_at
        else:
            second = quotes[1]
        self.fill(day, 1, second, signed[1], at_ms, opening=True)
        self.entry_side = entry_side
        self.opened = index
        self.entered += 1

    def start_unhedged(self, day, at_ms, phase, reason, leg_index):
        held = self.quantities[leg_index]
        quote = quote_at(day.legs[leg_index], at_ms)
        reference = -held * execution_price(quote, -1 if held > 0 else 1, self.stress)
        self.unhedged_since = (day.day, at_ms, self.cash, reference)
        self.record(day, "unhedged", leg=leg_index, phase=phase, at_ms=at_ms,
                    shares=held, reason=reason)

    def close_inventory(self, day, index, *, start, end):
        active = [(index, value) for index, value in enumerate(self.quantities) if value]
        for leg_index, holding in active:
            if not holding:
                continue
            at_ms, quote, reason = self.find_fill(day.legs[leg_index], -holding, start, end)
            if reason is not None:
                self.skip(day, reason, phase="close_inventory", leg=leg_index)
                continue
            self.fill(day, leg_index, quote, -holding, at_ms, opening=False)
            if not any(self.quantities):
                self.finish_exit(day, index, at_ms)

    def finish_exit(self, day, index, at_ms):
        if self.opened is not None:
            self.round_trips += 1
            self.holding_sessions.append(index - self.opened + 1)
        if self.unhedged_since is not None:
            start_day, start_ms, cash_at_start, reference = self.unhedged_since
            duration = (day.day - start_day).days * 86_400_000 + at_ms - start_ms
            proceeds = self.cash - cash_at_start
            interval = {"start_date": start_day.isoformat(), "start_ms": start_ms,
                        "end_date": day.day.isoformat(), "end_ms": at_ms, "duration_ms": duration,
                        "net_cash_during_flatten": proceeds,
                        "legging_pnl_vs_reference_fill": proceeds - reference}
            if self.opened is None:
                initial = self.ledger[
                    next(i for i in range(len(self.ledger) - 1, -1, -1)
                         if self.ledger[i]["event"] == "fill" and self.ledger[i]["opening"])
                ]
                initial_cost = initial["signed_shares"] * initial["price"] + initial["fee"]
                interval["failed_entry_net_pnl"] = proceeds - initial_cost
            self.unhedged_intervals.append(interval)
            self.record(day, "forced_flatten", **interval)
        self.record(day, "flat", at_ms=at_ms, reason=self.pending_exit)
        self.pending_exit = self.opened = self.entry_side = self.unhedged_since = None

    def close_pair(self, day, index, snapshot, *, start=OPEN_MS, deadline=WINDOW_END_MS):
        if not all(self.quantities):
            if any(self.quantities):
                self.close_inventory(day, index, start=max(start, REGULAR_OPEN_MS), end=day.close_ms)
            else:
                self.finish_exit(day, index, OPEN_MS)
            return
        if snapshot is None:
            self.skip(day, "missing_synchronized_quote", phase="exit")
            return
        at_ms, quotes = snapshot
        signed = tuple(-quantity for quantity in self.quantities)
        for leg_index, (leg, quote, quantity) in enumerate(zip(day.legs, quotes, signed, strict=True)):
            if (reason := fill_issue(leg, quote, quantity, at_ms, self.stress, opening=False)) is not None:
                self.skip(day, reason, phase="exit", leg=leg_index, at_ms=at_ms)
                return
        self.fill(day, 0, quotes[0], signed[0], at_ms, opening=False)
        if self.stress:
            filled_at, second, reason = self.find_fill(day.legs[1], signed[1], at_ms + LEG_DELAY_MS,
                                                       deadline)
            if reason is not None:
                self.start_unhedged(day, at_ms, "exit", reason, leg_index=1)
                self.skip(day, reason, phase="delayed_exit", leg=1)
                self.close_inventory(day, index, start=min(deadline + 1, day.close_ms), end=day.close_ms)
                return
            self.record_delay(day, "exit", 1, at_ms, filled_at, signed[1], quotes[1], second)
            at_ms = filled_at
        else:
            second = quotes[1]
        self.fill(day, 1, second, signed[1], at_ms, opening=False)
        self.finish_exit(day, index, at_ms)

    def recall_time(self, day, start, end):
        times = (event.at_ms for quantity, leg, opened in zip(self.quantities, day.legs, self.open_times, strict=True)
                 if quantity < 0 for event in leg.borrow
                 if event.recalled and max(start, opened) <= event.at_ms <= end)
        return min(times, default=None)

    def mark(self, day):
        net_value = self.cash
        gross_value = self.gross_cash
        for index, (quantity, leg) in enumerate(zip(self.quantities, day.legs, strict=True)):
            if not quantity:
                continue
            if leg.mark is None or not leg.mark.usable(day.close_ms):
                self.missing_evidence(day, "missing_liquidation_mark", leg=index)
                net_value = gross_value = None
                break
            side = 1 if quantity > 0 else -1
            half_spread = (leg.mark.ask - leg.mark.bid) / 2
            mark = leg.mark.midpoint - side * half_spread * (2 if self.stress else 1)
            if mark <= 0:
                self.missing_evidence(day, "invalid_liquidation_mark", leg=index)
                net_value = gross_value = None
                break
            net_value += quantity * mark
            gross_value += quantity * leg.mark.midpoint
        self.equity.append({"date": day.day.isoformat(), "net_equity": net_value,
                            "gross_equity": gross_value, "shares": list(self.quantities)})

    def run(self, sessions, previous_z):
        n = len(sessions)
        previous = None
        for index, day in enumerate(sessions):
            self.open_times[:] = (0, 0)
            self.last_fill = 0
            self.charge_holding_costs(day, previous)
            self.corporate_actions(day)
            snapshot = synchronized(day.legs)
            self.quote_days += snapshot is not None and all(min(q.bid_size, q.ask_size) > 0 for q in snapshot[1])
            opening_recall = self.recall_time(day, 0, OPEN_MS)
            if opening_recall is not None:
                self.pending_exit = "borrow_recall"
                self.record(day, "borrow_recall", at_ms=opening_recall)
                snapshot = synchronized(day.legs, OPEN_MS, day.close_ms)
            if self.pending_exit is not None:
                self.close_pair(day, index, snapshot, deadline=day.close_ms if opening_recall is not None else WINDOW_END_MS)
            if self.pending_entry is not None:
                self.open_pair(day, index, snapshot)

            recalled_at = self.recall_time(day, 0, day.close_ms)
            if recalled_at is not None and recalled_at != opening_recall:
                self.pending_exit = "borrow_recall"
                first_attempt = max(OPEN_MS, recalled_at, self.last_fill)
                self.record(day, "borrow_recall", at_ms=recalled_at)
                self.close_pair(day, index, synchronized(day.legs, first_attempt, day.close_ms),
                                start=first_attempt, deadline=day.close_ms)
            self.mark(day)
            if not day.stable and not self.disabled:
                self.disabled = True
                self.record(day, "stability_failure", at_ms=day.close_ms)
            if any(self.quantities) and self.pending_exit is None:
                if self.disabled:
                    self.pending_exit = "stability_failure"
                elif index >= n - 2:
                    self.pending_exit = "final_liquidation"
                elif self.opened is not None:
                    signed_z = self.entry_side * day.z
                    if signed_z <= .5:
                        self.pending_exit = "convergence"
                    elif signed_z >= 4:
                        self.pending_exit = "adverse_excursion"
                    elif index - self.opened + 1 >= MAX_HOLD:
                        self.pending_exit = "time_stop"
                if self.pending_exit is not None:
                    self.record(day, "exit_signal", at_ms=day.close_ms, reason=self.pending_exit)
            if not any(self.quantities) and not self.disabled and index < n - 2:
                if previous_z < 2 <= day.z:
                    self.pending_entry = 1
                elif previous_z > -2 >= day.z:
                    self.pending_entry = -1
                if self.pending_entry is not None:
                    self.record(day, "entry_signal", at_ms=day.close_ms, z=day.z, side=self.pending_entry)
            previous_z, previous = day.z, day

        if any(self.quantities):
            self.missing_evidence(sessions[-1], "open_terminal_inventory", shares=self.quantities)
        drawdown = peak = None
        if all(row["net_equity"] is not None for row in self.equity):
            drawdown, peak = 0.0, CAPITAL
            for row in self.equity:
                equity = row["net_equity"]
                peak = max(peak, equity)
                drawdown = max(drawdown, (peak - equity) / CAPITAL)
        return {
            "status": "unresolved" if self.missing else "complete", "stress": self.stress,
            "round_trips": self.round_trips, "entered": self.entered, "entry_attempts": self.entry_attempts,
            "entry_fill_fraction": self.entered / self.entry_attempts if self.entry_attempts else 0.0,
            "quote_coverage_fraction": self.quote_days / n, "turnover": self.turnover,
            "net_return": None if self.missing else self.cash / CAPITAL - 1,
            "gross_return": None if self.missing else self.gross_cash / CAPITAL - 1,
            "max_drawdown": None if self.missing else drawdown, "costs": self.costs,
            "stability_failure": self.disabled, "terminal_shares": self.quantities,
            "holding_sessions": self.holding_sessions, "unhedged_intervals": self.unhedged_intervals,
            "legging_intervals": self.legging_intervals,
            "reasons": dict(self.reasons), "missing_evidence": sorted(self.missing),
            "ledger": self.ledger, "equity": self.equity,
        }


def replay_calibration(sessions: Sequence[Session], *, beta: float, previous_z: float, stress=False) -> dict:
    """Replay the frozen candidate's signals using only ordered calibration sessions.

    Debit financing on marked long exposure and borrow on marked short exposure
    using the prior close rate through the next session (ACT/365 daily convention).
    When the stressed exit's delayed leg fails, liquidate the remaining open
    leg. Failed entry or exit flattening uses the earliest executable subsequent
    regular-session quote after the opening window, then retries next session.
    Stress sizes are frozen at the first opening snapshot for both legs.
    Reported leg durations use the input New York wall clock.
    """
    require_number(beta, "formation hedge", strict=True)
    if type(previous_z) not in (float, int) or not isfinite(previous_z) or type(stress) is not bool:
        raise ValueError("Invalid prior z-score or stress mode")
    rows = tuple(sessions)
    if len(rows) < 3 or any(not isinstance(row, Session) for row in rows):
        raise ValueError("Calibration replay requires at least three exchange sessions")
    days = tuple(row.day for row in rows)
    if days != tuple(sorted(set(days))) or not CALIBRATION_START <= days[0] <= days[-1] <= CALIBRATION_END:
        raise ValueError("Session dates fall outside the locked development calibration window")
    return Replay(beta, stress).run(rows, previous_z)
