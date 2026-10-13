"""CLI de quantbot.

python -m quantbot backtest     --config config/default.toml --data data/BTCUSDT_1h.csv
python -m quantbot backtest     --synthetic 8000 --montecarlo --stress 2.0
python -m quantbot walkforward  --data ... --grid "fast_ema=10,20,30;slow_ema=100,200"
python -m quantbot sensitivity  --data ... --grid "atr_stop_mult=2,2.5,3"
python -m quantbot validate-data --data ... --symbol BTC/USDT
python -m quantbot paper        --config config/paper.toml
python -m quantbot live         --config config/live.toml      (requiere QB_LIVE_CONFIRM)
python -m quantbot reset-kill-switch --operator maxi --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .config import AppConfig, Secrets, load_config
from .core.clock import timeframe_to_ms
from .core.logging_setup import setup_logging
from .core.models import Bar


def _parse_grid(spec: str) -> dict[str, list[Any]]:
    grid: dict[str, list[Any]] = {}
    for part in filter(None, spec.split(";")):
        key, values = part.split("=", 1)
        parsed: list[Any] = []
        for v in values.split(","):
            v = v.strip()
            parsed.append(int(v) if v.lstrip("-").isdigit() else float(v))
        grid[key.strip()] = parsed
    return grid


def _load_bars(args: argparse.Namespace, cfg: AppConfig) -> list[Bar]:
    tf_ms = timeframe_to_ms(cfg.strategy.timeframe)
    if args.synthetic:
        from .data.synthetic import generate_bars

        out: list[Bar] = []
        for i, sym in enumerate(cfg.strategy.symbols):
            out += generate_bars(sym, args.synthetic, tf_ms, seed=args.seed + i)
        return out
    if not args.data:
        sys.exit("Indicar --data <archivo> o --synthetic N")
    from .data.historical import load_csv, load_parquet

    bars: list[Bar] = []
    symbols = args.symbol.split(",") if args.symbol else list(cfg.strategy.symbols)
    paths = args.data.split(",")
    if len(paths) != len(symbols):
        sys.exit("--data y --symbol deben tener la misma cantidad de elementos")
    for path, sym in zip(paths, symbols, strict=True):
        loader = load_parquet if path.endswith(".parquet") else load_csv
        b, report = loader(path, sym, tf_ms)
        print(f"[datos] {report.summary()}")
        if not report.ok:
            sys.exit("Datos inválidos: corregir antes de backtestear")
        bars += b
    return bars


def cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest.engine import run_backtest
    from .backtest.montecarlo import monte_carlo

    cfg = load_config(args.config, mode="backtest")
    bars = _load_bars(args, cfg)
    res = run_backtest(cfg, bars, cost_stress=args.stress, n_trials=args.trials)
    print(f"\n=== BACKTEST ({len(bars)} barras, stress costos x{args.stress}) ===")
    print(res.report.pretty())
    print(f"  órdenes={res.orders_sent} vetos={res.vetoes} fees={res.fees_paid:.2f}")
    if res.killed:
        print(f"  🚨 KILL SWITCH durante el backtest: {res.kill_reason}")
    if args.montecarlo and res.trade_returns:
        mc = monte_carlo(res.trade_returns, kill_threshold=cfg.risk.total_drawdown_kill)
        print("\n=== MONTECARLO ===")
        print(mc.pretty())
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "report": res.report.as_dict(),
                    "equity": list(zip(res.equity_ts, res.equity, strict=True)),
                    "trades": [(t.ts, t.symbol, t.pnl) for t in res.trades],
                },
                default=str,
            )
        )
        print(f"\nResultados guardados en {args.out}")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    from .backtest.walk_forward import walk_forward

    cfg = load_config(args.config, mode="backtest")
    bars = _load_bars(args, cfg)
    wf = walk_forward(cfg, bars, _parse_grid(args.grid), args.train, args.test, args.warmup)
    print(f"\n=== WALK-FORWARD ({len(wf.windows)} ventanas, {wf.n_trials} pruebas) ===")
    for i, w in enumerate(wf.windows, 1):
        print(
            f"  #{i:<2} params={w.best_params} train_SR={w.train_sharpe:.2f} "
            f"test_SR={w.test.report.sharpe:.2f} test_DD={w.test.report.max_drawdown:.2%}"
        )
    print("\n  Estabilidad de parámetros:", wf.param_stability())
    print("\n=== OUT-OF-SAMPLE CONCATENADO ===")
    print(wf.oos_report.pretty())
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    from .backtest.walk_forward import parameter_sensitivity

    cfg = load_config(args.config, mode="backtest")
    bars = _load_bars(args, cfg)
    rows = parameter_sensitivity(cfg, bars, _parse_grid(args.grid))
    print("\n=== SENSIBILIDAD (buscar meseta, no pico) ===")
    for params, sr, dd, n in sorted(rows, key=lambda r: -r[1]):
        print(f"  {params}  SR={sr:6.2f}  DD={dd:6.2%}  trades={n}")
    return 0


def cmd_validate_data(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, mode="backtest")
    _load_bars(args, cfg)
    return 0


def cmd_run(args: argparse.Namespace, mode: str) -> int:
    from .app import TradingApp

    cfg = load_config(args.config, mode=mode)
    setup_logging(cfg.log_level)
    app = TradingApp(cfg, Secrets.from_env())
    return asyncio.run(app.run())


def cmd_reset_kill(args: argparse.Namespace) -> int:
    from .core.kill_switch import KillSwitch, human_reset

    if not args.confirm:
        sys.exit("Agregar --confirm. Antes: revisar logs, posiciones y la causa del kill.")
    ks = KillSwitch(args.state_dir)
    if ks.record:
        print(f"Kill activo: {ks.record.reason}")
    if human_reset(args.state_dir, args.operator):
        print(f"Kill-switch rearmado por {args.operator} (auditado en kill_switch_audit.log)")
    else:
        print("No había kill-switch activo.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="quantbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    def data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default=None)
        sp.add_argument("--data", help="CSV/Parquet (separados por coma si multi-activo)")
        sp.add_argument("--symbol", help="Símbolos correspondientes a --data")
        sp.add_argument("--synthetic", type=int, default=0, help="N barras sintéticas")
        sp.add_argument("--seed", type=int, default=42)

    bt = sub.add_parser("backtest")
    data_args(bt)
    bt.add_argument("--stress", type=float, default=1.0, help="Multiplicador de costos")
    bt.add_argument("--montecarlo", action="store_true")
    bt.add_argument("--trials", type=int, default=1, help="Pruebas hechas (para Deflated SR)")
    bt.add_argument("--out")

    wf = sub.add_parser("walkforward")
    data_args(wf)
    wf.add_argument("--grid", required=True)
    wf.add_argument("--train", type=int, default=3000)
    wf.add_argument("--test", type=int, default=1000)
    wf.add_argument("--warmup", type=int, default=600)

    se = sub.add_parser("sensitivity")
    data_args(se)
    se.add_argument("--grid", required=True)

    vd = sub.add_parser("validate-data")
    data_args(vd)

    for mode in ("paper", "live", "demo"):
        sp = sub.add_parser(mode)
        sp.add_argument("--config", default=None)

    rk = sub.add_parser("reset-kill-switch")
    rk.add_argument("--state-dir", default="./state")
    rk.add_argument("--operator", required=True)
    rk.add_argument("--confirm", action="store_true")

    args = p.parse_args(argv)
    if args.cmd not in ("paper", "live", "demo"):
        setup_logging("WARNING", json_output=False)
    match args.cmd:
        case "backtest":
            return cmd_backtest(args)
        case "walkforward":
            return cmd_walkforward(args)
        case "sensitivity":
            return cmd_sensitivity(args)
        case "validate-data":
            return cmd_validate_data(args)
        case "paper" | "live" | "demo":
            return cmd_run(args, args.cmd)
        case "reset-kill-switch":
            return cmd_reset_kill(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
