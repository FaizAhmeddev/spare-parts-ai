"""Command-line interface.

  python -m spareai run                 full pipeline (load -> analyse -> report)
  python -m spareai validate            schema + data-quality checks only
  python -m spareai serve               web dashboard + REST API on :8010
  python -m spareai top --action CLEAR  print top recommendations
  python -m spareai part P0045          explain one part
"""
from __future__ import annotations

import sys

import click
import pandas as pd

from .config import load_settings


@click.group()
@click.option("--config", "config_path", default=None, help="Path to settings.yaml")
@click.pass_context
def cli(ctx, config_path):
    """SpareAI - spare parts inventory intelligence."""
    ctx.obj = load_settings(config_path)


@cli.command()
@click.option("--source", type=click.Choice(["excel", "csv", "sql"]), help="Override source.type")
@click.option("--as-of", default=None, help="Analysis date YYYY-MM-DD (default: last data date)")
@click.option("--no-ml", is_flag=True, help="Skip the machine-learning forecaster (faster)")
@click.option("--no-backtest", is_flag=True, help="Skip the 12-month policy simulation")
@click.pass_obj
def run(settings, source, as_of, no_ml, no_backtest):
    """Run the full pipeline."""
    from .pipeline import run_pipeline
    if source:
        settings.override("source.type", source)
    if as_of:
        settings.override("analysis.as_of_date", as_of)
    use_ml = bool(settings.get("forecasting.use_ml", True)) and not no_ml
    res = run_pipeline(settings, use_ml=use_ml, backtest=not no_backtest)
    s = res["summary"]
    click.echo("\n" + "=" * 64)
    click.echo(f" SpareAI run {res['run_id']}  (data as of {s['as_of_date']})")
    click.echo("=" * 64)
    click.echo(f" Inventory value          ${s['inventory_value_usd']:>14,.0f}")
    click.echo(f" Dead/obsolete stock      ${s['dead_stock_value_usd']:>14,.0f}  ({s['dead_stock_pct']:.1%})")
    click.echo(f" Value to clear           ${s['clear_value_usd']:>14,.0f}")
    click.echo(f" Value to buy             ${s['reorder_value_usd']:>14,.0f}")
    click.echo(f" Holding saving / year    ${s['annual_saving_usd']:>14,.0f}")
    click.echo(f" Critical reorders         {s['critical_reorders']:>14}")
    click.echo(" Actions: " + ", ".join(f"{k}={v}" for k, v in sorted(s["actions"].items())))
    if "backtest" in s:
        ai, lg = s["backtest"]["AI policy"], s["backtest"]["Current min/max"]
        click.echo(f" Backtest fill rate        AI {ai['fill_rate']:.1%}  vs current {lg['fill_rate']:.1%}")
        click.echo(f" Backtest avg inventory    AI ${ai['avg_inventory_value_usd']:,.0f}  vs current "
                   f"${lg['avg_inventory_value_usd']:,.0f}")
    for f in s.get("reports", []):
        click.echo(f" Report: {f}")
    click.echo(f" Dashboard: python -m spareai serve  ->  http://localhost:{settings.get('server.port', 8010)}")


@cli.command()
@click.pass_obj
def validate(settings):
    """Check the source against the data contract and run data-quality checks."""
    from .ingestion.loader import SchemaError, load_dataset
    from .ingestion.sources import build_source
    from .pipeline import resolve_as_of
    from .quality.checks import run_quality_checks
    try:
        ds = load_dataset(build_source(settings))
    except (SchemaError, FileNotFoundError, KeyError) as exc:
        click.secho(f"Schema check FAILED: {exc}", fg="red")
        sys.exit(1)
    click.secho("Schema check passed: " + ", ".join(f"{k}={v}" for k, v in ds.row_counts().items()), fg="green")
    dq, dup = run_quality_checks(ds, resolve_as_of(ds, settings), settings)
    click.echo(dq.groupby(["severity", "check"]).size().to_string())
    if len(dup):
        click.echo("\nDuplicate part codes:")
        click.echo(dup[["duplicate_part_id", "master_part_id", "reason"]].to_string(index=False))
    sys.exit(2 if (dq["severity"] == "ERROR").any() else 0)


@cli.command()
@click.option("--host", default=None, help="Default: server.host in settings.yaml")
@click.option("--port", default=None, type=int, help="Default: server.port in settings.yaml (8010)")
@click.pass_obj
def serve(settings, host, port):
    """Start the dashboard and REST API."""
    import socket
    from .web.app import create_app
    host = host or settings.get("server.host", "127.0.0.1")
    port = int(port or settings.get("server.port", 8010))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            click.secho(f"Port {port} is already in use by another application.\n"
                        f"Use another one:  python -m spareai serve --port {port + 1}\n"
                        f"or set SPAREAI_PORT / server.port in config/settings.yaml", fg="red")
            sys.exit(1)
    app = create_app(settings)
    try:
        from waitress import serve as wserve  # production WSGI server if installed
        click.echo(f"SpareAI on http://{host}:{port} (waitress)")
        wserve(app, host=host, port=port)
    except ImportError:
        click.echo(f"SpareAI on http://{host}:{port}")
        app.run(host=host, port=port, debug=False)


def _latest(settings):
    from .storage.warehouse import Warehouse
    wh = Warehouse(settings.path("warehouse.path"))
    run = wh.latest_run()
    if not run:
        click.secho("No successful run yet. Run: python -m spareai run", fg="yellow")
        sys.exit(1)
    return wh, run


@cli.command()
@click.option("--action", default=None)
@click.option("-n", default=20, type=int)
@click.pass_obj
def top(settings, action, n):
    """Print top recommendations from the latest run."""
    wh, run = _latest(settings)
    r = wh.read("recommendations", run["run_id"])
    r = r[r["action"] == action] if action else r[r["action"] != "OK"]
    pd.set_option("display.width", 200)
    click.echo(r.head(n)[["action", "urgency", "part_id", "site_id", "description",
                          "recommended_qty", "value_usd"]].to_string(index=False))


@cli.command()
@click.argument("part_id")
@click.pass_obj
def part(settings, part_id):
    """Explain the recommendation for one part."""
    wh, run = _latest(settings)
    r = wh.read("recommendations", run["run_id"], where="part_id = ?", params=(part_id,))
    if r.empty:
        click.secho("Part not found", fg="red")
        sys.exit(1)
    for row in r.itertuples():
        click.secho(f"\n{row.part_id} @ {row.site_id} - {row.description}", bold=True)
        click.echo(f"  {row.action} {row.sub_action} {row.urgency}  qty={row.recommended_qty}  "
                   f"value=${row.value_usd:,.0f}  confidence={row.confidence}")
        for reason in row.reason.split(" | "):
            click.echo(f"   - {reason}")


def main():
    cli(prog_name="spareai")


if __name__ == "__main__":
    main()
