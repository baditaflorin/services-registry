#!/usr/bin/env python3
"""verify-examples-metrics: turn services.examples.json into a Prometheus textfile and name what regressed.

  verify-examples-metrics.py --new FILE [--old FILE] --out PROM [--failed]

Metrics (node_exporter textfile format):
  fleet_example_state{service,state} 1       the current state of every checked service (one series per service)
  fleet_examples_services{state}     N       how many services are in each state
  fleet_examples_regressed           N       services that were `ok` in --old and are now broken (error, unreachable, not-found,
                                             rejected, missing-param, method). `gated` and `no-data` are not "broken": the first
                                             says the key was not accepted, the second that the route is right and the service
                                             has no data for the example input.
  fleet_examples_last_run_timestamp_seconds, fleet_examples_run_success  (0 with --failed: the run did not produce a result)
Prints the regressed ids on stdout, one per line. stdlib only.
"""
import argparse
import json
import os
import sys
import time

BROKEN = {"error", "unreachable", "not-found", "rejected", "missing-param", "method"}


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def regressed(old, new):
    return sorted(k for k, r in new.items() if r.get("state") in BROKEN and old.get(k, {}).get("state") == "ok")


def render(new, old, success=True, now=None):
    now = int(now if now is not None else time.time())
    reg = regressed(old, new)
    tally = {}
    for r in new.values():
        tally[r.get("state", "unknown")] = tally.get(r.get("state", "unknown"), 0) + 1
    out = ["# HELP fleet_example_state Current state of each service's registry example call (1 = this state).",
           "# TYPE fleet_example_state gauge"]
    for k in sorted(new):
        out.append('fleet_example_state{service="%s",state="%s"} 1' % (esc(k), esc(new[k].get("state", "unknown"))))
    out += ["# HELP fleet_examples_services Services per example state.", "# TYPE fleet_examples_services gauge"]
    for st in sorted(tally):
        out.append('fleet_examples_services{state="%s"} %d' % (esc(st), tally[st]))
    out += ["# HELP fleet_examples_regressed Services that were ok at the previous run and are broken now.",
            "# TYPE fleet_examples_regressed gauge", "fleet_examples_regressed %d" % len(reg),
            "# TYPE fleet_examples_last_run_timestamp_seconds gauge", "fleet_examples_last_run_timestamp_seconds %d" % now,
            "# TYPE fleet_examples_run_success gauge", "fleet_examples_run_success %d" % (1 if success else 0)]
    return "\n".join(out) + "\n", reg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--new", required=True)
    ap.add_argument("--old", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--failed", action="store_true", help="the run produced no result: write only the failure metrics")
    a = ap.parse_args(argv)
    if a.failed:
        text = ("# TYPE fleet_examples_run_success gauge\nfleet_examples_run_success 0\n"
                "# TYPE fleet_examples_last_run_timestamp_seconds gauge\nfleet_examples_last_run_timestamp_seconds %d\n" % int(time.time()))
        reg = []
    else:
        new = json.load(open(a.new)).get("services", {})
        old = json.load(open(a.old)).get("services", {}) if a.old and os.path.exists(a.old) else {}
        text, reg = render(new, old)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, a.out)               # node_exporter must never read a half-written file
    for k in reg:
        print(k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
