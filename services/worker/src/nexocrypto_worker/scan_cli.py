"""Scanner CLI — run scan_once against live Bitunix on a tick interval.

  python -m nexocrypto_worker.scan_cli BTCUSDT
  python -m nexocrypto_worker.scan_cli BTCUSDT --interval 30 --ticks 5
  python -m nexocrypto_worker.scan_cli ETHUSDT --bars 500 --tf 15m

CLAUDE.md compliance: PAPER mode only, no order placement, deterministic decisions
printed with reasons. No LLM in the path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from nexocrypto_connectors.bitunix import BitunixConnector

from .scanner import ScanResult, scan_once


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def _render(result: ScanResult) -> str:
    lines: list[str] = []
    lines.append(
        f"[{result.taken_at.isoformat(timespec='seconds')}] {result.pair}  "
        f"mark={result.mark_price}  funding={result.funding_rate}  "
        f"bars={result.kline_count}"
    )
    for o in result.outcomes:
        if not o.fired:
            lines.append(f"  {o.strategy_key:<24}  no signal")
            continue
        sig = o.signal
        dec = o.decision
        verdict = "APPROVED" if dec and dec.approved else f"REJECTED ({dec.reason})"
        lines.append(
            f"  {o.strategy_key:<24}  {sig.side.value:<5} "
            f"entry={sig.entry} sl={sig.stop_loss} tp={sig.take_profits[0]}  -> {verdict}"
        )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    venue = BitunixConnector()
    try:
        for tick in range(1, args.ticks + 1):
            try:
                result = await scan_once(
                    venue, args.symbol, interval=args.tf, bars=args.bars
                )
                print(f"--- tick {tick}/{args.ticks} ---")
                print(_render(result), flush=True)
            except Exception as e:
                print(f"--- tick {tick}/{args.ticks} ERROR: {type(e).__name__}: {e} ---", flush=True)
            if tick < args.ticks:
                await asyncio.sleep(args.interval)
    finally:
        await venue.aclose()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run the scanner against live Bitunix.")
    p.add_argument("symbol", help="trading pair, e.g. BTCUSDT")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between ticks")
    p.add_argument("--ticks", type=int, default=1, help="number of ticks to run")
    p.add_argument("--tf", default="5m", help="kline timeframe")
    p.add_argument("--bars", type=int, default=300, help="kline lookback")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
