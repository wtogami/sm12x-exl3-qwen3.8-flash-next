#!/usr/bin/env python3
"""Compare two read-matrix output directories cell by cell.

Exists because the summary was previously inlined as python inside an ssh command string, and the
quoting layers mangled f-strings containing quoted keys -- twice now, including once where the
crash hid a result that was already visible in the raw columns. A committed script either works or
raises where you can see it.
"""
import argparse, glob, json, os, sys


def load(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            j = json.load(open(f))
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr); continue
        if isinstance(j, dict) and "mode" in j:
            out[os.path.basename(f)[:-5]] = j
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir_a")
    ap.add_argument("dir_b")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--requests", type=int, default=0,
                    help="row requests per cell; if 0, taken from the receipt's raw_row_requests")
    ap.add_argument("--noise", type=float, default=0.01,
                    help="device-page agreement fraction under which the arms are indistinguishable")
    a, b = load(ap.dir_a), load(ap.dir_b)
    shared = sorted(set(a) & set(b))
    if not shared:
        print("  no cells in common -- nothing to compare"); return 1
    print(f"  {'cell':20s} {'dev A':>12s} {'dev B':>12s} {'ddev':>9s} "
          f"{'p/s A':>9s} {'p/s B':>9s} {'drate':>7s} {'hit A':>7s} {'hit B':>7s}")
    worst_dev = worst_rate = 0.0
    for tag in shared:
        ja, jb = a[tag], b[tag]
        da = ja.get("device_pages_net") or ja.get("device_pages_read") or 0
        db = jb.get("device_pages_net") or jb.get("device_pages_read") or 0
        ra, rb = ja.get("pages_per_s_achieved", 0), jb.get("pages_per_s_achieved", 0)
        n = ap.requests or ja.get("raw_row_requests") or 0
        ha = f"{1 - da / n:.3f}" if n else "-"
        hb = f"{1 - db / n:.3f}" if n else "-"
        ddev = abs(da - db) / max(1, da)
        drate = abs(ra - rb) / max(1e-9, ra)
        worst_dev, worst_rate = max(worst_dev, ddev), max(worst_rate, drate)
        print(f"  {tag:20s} {da:12,d} {db:12,d} {ddev:8.4%} {ra:9,.0f} {rb:9,.0f} "
              f"{drate:6.2%} {ha:>7s} {hb:>7s}")
    print(f"\n  worst device-page disagreement: {worst_dev:.4%}   worst rate difference: {worst_rate:.2%}")
    if worst_dev < ap.noise:
        print(f"  verdict: retention indistinguishable (<{ap.noise:.0%}); the arms do not differ in what")
        print("           the page cache kept. Any surviving rate difference is submit-path, not layout.")
    else:
        print(f"  verdict: retention differs by up to {worst_dev:.2%} -- a real effect between the arms.")
    print(f"  also note: identical arms imply run-to-run noise is <= {worst_dev:.4%} on device pages,")
    print(f"             which sets the floor for calling any past {worst_dev*200:.2f}% difference signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
