#!/usr/bin/env python3
"""Position-binding probe (NIAT-style): query table cells by row/column index.

The haystack is a generated table whose cells are random tokens; the query
names cells by (row, column) coordinates, so a correct answer requires binding
an ordinal/labelled position to content -- no lexical bridge exists between
question and answer.  Two variants: "labeled" rows carry an explicit id token
(search plus position binding); "ordinal" rows carry no ids, so row N is the
Nth data row and the model must count (pure positional arithmetic, the arm
most sensitive to rope-scaling changes).  Scoring is reported per-needle with
presence and order-aligned variants; lengths run in the order given so a
worst-first schedule reports the deepest cells first.
"""
import argparse
import json
import random
import string
import time
import urllib.request
from pathlib import Path


def post_json(base_url, path, payload, timeout=1800):
    request = urllib.request.Request(base_url.rstrip("/") + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


COLUMNS = list("ABCDEFGH")


def build_table(rng, rows, labeled):
    lines = []
    for i in range(1, rows + 1):
        cells = ["".join(rng.choices(string.hexdigits.lower(), k=6)) for _ in COLUMNS]
        prefix = f"R{i:04d}:" if labeled else "|"
        lines.append(f"{prefix} " + " | ".join(cells) + " |")
    return "\n".join(lines)


def fit_rows(base_url, model, target_tokens, labeled, seed):
    rng = random.Random(f"{seed}|fit")
    sample = build_table(rng, 50, labeled)
    per_row = post_json(base_url, "/tokenize", {"model": model, "prompt": sample})["count"] / 50
    rows = max(int((target_tokens - 120) / per_row), 10)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--lengths", type=int, nargs="+", default=[229376, 131072, 65536, 16384, 4096])
    parser.add_argument("--variants", nargs="+", default=["ordinal", "labeled"])
    parser.add_argument("--cells", type=int, default=2, help="cells queried per sample")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--seed", type=int, default=787)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/").removesuffix("/v1")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("x") as output:
        def emit(row):
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()

        emit({"record": "meta", "schema": "qwen38-table-position-v1",
              "args": {k: str(v) for k, v in vars(args).items()},
              "method": "NIAT-style coordinate queries over generated random-cell tables; "
                        "ordinal rows require counting, labeled rows require id search; "
                        "presence and order-aligned scoring"})
        for variant in args.variants:
            for length in args.lengths:
                rows = fit_rows(base, args.model, length, variant == "labeled", args.seed)
                present = aligned = answered = skipped = 0
                elapsed = []
                for s in range(args.samples):
                    rng = random.Random(f"{args.seed}|{variant}|{length}|{s}")
                    table = build_table(rng, rows, variant == "labeled")
                    picks = rng.sample(range(1, rows + 1), args.cells)
                    cols = [rng.choice(COLUMNS) for _ in picks]
                    table_lines = table.split("\n")
                    golds = []
                    for row_index, col in zip(picks, cols):
                        line = table_lines[row_index - 1]
                        if variant == "labeled":
                            line = line.split(": ", 1)[1]
                        value = line.strip("| ").split(" | ")[COLUMNS.index(col)]
                        golds.append(value)
                    coords = ", ".join(f"row {r}, column {c}" for r, c in zip(picks, cols))
                    prompt = ("The table below lists random cell values. Columns are labeled "
                              + " ".join(COLUMNS) + " from left to right."
                              + (" Each row starts with its row id (R####)." if variant == "labeled"
                                 else f" Rows carry NO ids: row N is the Nth data row, counting from 1.")
                              + f"\nGive the exact value of these cells: {coords}. "
                                 "Return one value per line, in the order asked, nothing else.\n\n"
                              + table)
                    started = time.perf_counter()
                    try:
                        result = post_json(base, "/v1/chat/completions",
                                           {"model": args.model, "temperature": 0, "max_tokens": 16 * args.cells,
                                            "messages": [{"role": "user", "content": prompt}],
                                            "chat_template_kwargs": {"enable_thinking": False}})
                    except Exception as exc:  # noqa: BLE001
                        if "400" in str(exc):
                            skipped += 1
                            emit({"record": "measurement", "variant": variant, "length": length,
                                  "sample": s, "skipped": "over-window", "error": str(exc)[:160]})
                            continue
                        raise
                    content = result["choices"][0]["message"]["content"].strip()
                    lines = [x.strip() for x in content.splitlines() if x.strip()]
                    hits = [g in content for g in golds]
                    aln = all(i < len(lines) and golds[i] in lines[i] for i in range(len(golds)))
                    present += sum(hits)
                    aligned += aln
                    answered += 1
                    elapsed.append(time.perf_counter() - started)
                    emit({"record": "measurement", "variant": variant, "length": length, "sample": s,
                          "coords": coords, "gold": golds, "response": content[:200],
                          "presence": hits, "aligned": aln, "usage": result["usage"],
                          "seconds": round(time.perf_counter() - started, 2)})
                cell = {"record": "cell", "variant": variant, "length": length, "rows": rows,
                        "samples": args.samples, "answered": answered, "over_window_skipped": skipped,
                        "presence_rate": round(present / max(answered * args.cells, 1), 4),
                        "aligned_rate": round(aligned / max(answered, 1), 4),
                        "median_seconds": round(sorted(elapsed)[len(elapsed) // 2], 2) if elapsed else None}
                emit(cell)
                print(f"{variant} len={length}: presence={cell['presence_rate']} "
                      f"aligned={cell['aligned_rate']} rows={rows} skipped={skipped}", flush=True)


if __name__ == "__main__":
    main()
