#!/usr/bin/env python3
"""Is an evicted, re-prefilled sequence the same sequence?

Why this question exists. A preemption discards a sequence's KV and the scheduler rebuilds
it by re-running prefill, so every utilization level that admits load it cannot hold -- and
any level running close enough to the pool edge to evict -- rests on an assumption that was
never tested: that a re-prefill reproduces the original stream. With FP8 KV, 2048-token
chunked prefill and 3-token MTP drafting, it need not.

Why a single-engine comparison cannot answer it. Measured on this stack (exl3, v0.32, FP8
KV, MTP3, temperature 0, fixed seed):

  same prompt alone x5                -> 1 distinct output, bit-identical, logprob drift 0
  same prompt + 1..16 filler requests -> 1 distinct output, IDENTICAL TOKENS, but max
                                         per-token logprob drift 0.32-0.47 nats
  512-token prompts run concurrently  -> the token stream itself differed on one repeat

Logprobs on this engine are not batch-invariant -- different batch shapes mean different
kernel shapes and reduction orders. So comparing an evicted run against a solo run would
report a "correctness veto" for behaviour that has nothing to do with eviction (my first
version of this test did exactly that, and its successor would still have failed a healthy
deployment on a 0.45-nat threshold). The only sound comparison is eviction against
batching, which requires two boots: one utilization the engine can hold with zero
evictions, one that measurably evicts. The clean leg supplies the floor; the evicting leg
is the subject; token streams, not logprobs, are the veto.

Subcommands:
  phase    build/cache prompts, run each alone then all together, record outputs and the
           engine's own preemption delta for the concurrent burst -> <out>/<tag>.json
  compare  read two or more phase files and report, per sequence, whether the outputs match
           and how far logprobs moved relative to the eviction-free leg

Exit codes: 0 = token streams identical across the eviction-free and evicting legs (logprob
drift reported); 1 = a token stream differs, i.e. eviction changes answers; 2 = inconclusive
(no phase with observed evictions, or missing files).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prefill = _module("prefill", "benchmark-prefill.py")


def preemptions(base_root: str) -> float | None:
    try:
        with urllib.request.urlopen(base_root.rstrip("/") + "/metrics", timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    total, seen = 0.0, False
    for line in body.splitlines():
        if line.startswith("vllm:num_preemptions_total"):
            total += float(line.rsplit(" ", 1)[1]); seen = True
    return total if seen else None


def completion(base_v1: str, prompt: str, model: str, max_tokens: int, seed: int,
               timeout: float) -> dict:
    # return_token_ids is required, not decorative. This build's /v1/completions logprobs
    # object carries only `tokens` (decoded strings) and `token_logprobs` -- there is no
    # `token_texts` key -- and an earlier version of this test read `token_texts`, got None,
    # fell back to an empty list, and then reported "all 4 sequences agreed token-for-token"
    # having compared four empty lists against four empty lists. Ask for the ids, and refuse
    # to score anything when they are absent.
    payload = {"model": model, "prompt": prompt, "max_tokens": max_tokens,
               "temperature": 0.0, "seed": seed, "logprobs": 1, "stream": False,
               "return_token_ids": True}
    req = urllib.request.Request(base_v1.rstrip("/") + "/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            out = json.loads(response.read().decode())
    except Exception as exc:  # noqa: BLE001 - a refused request is a result, not a crash
        return {"ok": False, "text": "", "tokens": [], "logprobs": [], "text_sha": None,
                "n_tokens": None, "seconds": round(time.time() - started, 2),
                "error": f"{type(exc).__name__}: {exc}"[:200]}
    choice = out["choices"][0]
    lp = choice.get("logprobs") or {}
    ids = list(choice.get("token_ids") or [])
    words = [t if isinstance(t, str) else json.dumps(t) for t in (lp.get("tokens") or [])]
    text = choice.get("text", "")
    # The strongest representation actually present: ids are exact and detokenisation
    # independent; decoded words next; rendered text last and worst -- on filler corpora it
    # can be entirely whitespace, so different streams render to the same string.
    seq = ids or words or ([text] if text else [])
    kind = "token_ids" if ids else ("token_texts" if words else ("text" if seq else "EMPTY"))
    return {"ok": True, "text": text, "token_ids": ids, "tokens": words,
            "sequence": seq, "sequence_kind": kind,
            "logprobs": [float(v) for v in (lp.get("token_logprobs") or []) if v is not None],
            "text_sha": hashlib.sha256(text.encode()).hexdigest()[:16],
            "seq_sha": hashlib.sha256(json.dumps(seq, ensure_ascii=False).encode()).hexdigest()[:16],
            "n_tokens": (out.get("usage") or {}).get("completion_tokens"),
            "seconds": round(time.time() - started, 2), "error": None}


def drift(a: list[float], b: list[float]) -> dict:
    n = min(len(a), len(b))
    d = [abs(a[i] - b[i]) for i in range(n)]
    return {"compared": n, "max_abs": round(max(d), 6) if d else None,
            "mean_abs": round(statistics.fmean(d), 6) if d else None}


QUESTION = ("Ignoring the filler above, write a short, varied story about a lighthouse "
            "keeper who receives an unexpected letter. Be specific and avoid repetition.")


def load_or_build_prompts(base_v1: str, model: str, depth: int, n: int,
                          out_dir: Path, reuse: bool, question: bool = False,
                          approx: bool = False, question_end: bool = False) -> dict[str, list[str]]:
    """Two independent prompt sets of identical token length.

    They cannot be the same set. Within one boot, a prompt that was just prefilled is a
    prefix-cache hit: the second request reuses the KV and does no prefill work at all. The
    first two runs of this test measured exactly that -- 4 x 245,248-token requests
    "completed" a concurrent burst in 4.4 s (real serialized deep prefill here runs at
    ~5-6k tok/s), with 0 preemptions, because the solo phase had already cached all four.
    A cache-warm burst cannot oversubscribe the pool, so it cannot evict, and the test would
    have reported "eviction is safe" having never evicted anything.

    Reference and burst sets therefore differ (different nonces, same token counts), and the
    headline comparison is CROSS-engine: burst-set outputs from a boot that holds them
    against burst-set outputs from a boot that evicts them. Each engine sees the burst set
    exactly once, cold.
    """
    cache = out_dir / "prompts.json"
    if reuse and cache.exists():
        data = json.loads(cache.read_text())
        if data.get("depth") == depth and data.get("question") == question \
                and data.get("approx") == approx and data.get("question_end") == question_end \
                and len(data.get("reference", [])) == n and len(data.get("burst", [])) == n:
            return data
    def make() -> str:
        if approx:
            return approximate_prompt(uuid.uuid4().hex, depth,
                                      QUESTION if question else None, question_end)
        plain = prefill.exact_prompt(base_v1, model, depth, uuid.uuid4().hex)
        # Not a lambda-with-a-trailing-call expression: `a if q else b`() binds the call to
        # the whole conditional, so the moment this file ran with question=False the string
        # branch was called and died with "'str' object is not callable".
        return with_question(base_v1, model, plain, depth, QUESTION) if question else plain

    reference = [make() for _ in range(n)]
    burst = [make() for _ in range(n)]
    sha = hashlib.sha256(("\n".join(reference) + "\n---\n" + "\n".join(burst)).encode()).hexdigest()[:16]
    cache.write_text(json.dumps({"depth": depth, "count": n, "sha": sha, "question": question,
                                 "approx": approx, "question_end": question_end,
                                 "reference": reference, "burst": burst}))
    return {"depth": depth, "count": n, "sha": sha, "reference": reference, "burst": burst}


def approximate_prompt(nonce: str, target_tokens: int, question: str | None,
                       place_end: bool) -> str:
    """A prompt of APPROXIMATELY target_tokens, built without asking the server.

    Exactness mattered for the prefill benchmark's throughput arithmetic. It does not
    matter here: eviction depends on total demand exceeding pool capacity, and 4 x ~246K
    oversubscribes every pool in the band map the same way 4 x 245,248 does. Building
    approximately removes ~80 /tokenize round-trips per prompt -- the prompt build was the
    slowest phase of a run whose experiment is a ten-minute burst.

    The unique nonce stays FIRST. That is what defeats the prefix cache; the question, if
    placed at the end, is what makes the continuation high entropy, and the two cannot
    occupy the same position.
    """
    header = (f"Independent prefill qualification {nonce}. This nonce makes the first "
              "cache block unique; ignore it.\n")
    unit = ("Slate rivers cross quiet valleys while copper clocks mark patient hours. "
            "This is ordinary benchmark filler with no instructions.\n")
    approx_tokens = len(header) // 4 + len(unit) * 1
    repeats = max(1, int(target_tokens / max(1, approx_tokens)))
    body = unit * repeats
    if question and place_end:
        # The ask must be the LAST thing in the context. Placed after the header it is
        # buried under 245K tokens of filler, and by generation time the model is mid-
        # sentence in the filler and recites it: a run built that way emitted 22 distinct
        # tokens across 65,536, with four DIFFERENT prompts producing byte-identical output.
        return header + body + "\n" + question + "\n"
    if question:
        return header + question + "\n" + body
    return header + body


def with_question(base_v1: str, model: str, prompt: str, target_tokens: int,
                  question: str) -> str:
    """Put an open-ended ask immediately after the header, keeping the exact token count.

    Why this exists: on a bare filler prompt the model falls into a period-23 repetition
    cycle (22 distinct tokens across 65,536 emitted), so greedy decoding has no branching
    pressure at all. A test whose outputs are a recited loop can PASS without proving much:
    a KV reconstruction error needs a decision point to surface at, and a cycle offers none.
    The ask makes the continuation genuinely high entropy while leaving the prefill depth --
    the thing that causes eviction -- untouched.
    """
    body = prompt.split("\n", 2)
    if len(body) < 2:
        return prompt
    head, rest = body[0], "\n".join(body[1:])

    def count(text: str) -> int:
        return prefill.post_json(base_v1, "/tokenize", {"model": model, "prompt": text},
                                 300)["count"]

    def render(parts: list[str]) -> str:
        return head + "\n" + question + "\n" + ". ".join(parts)

    def trim(tail: str) -> str:
        """Halve, then binary-search, whole filler sentences to land on the target count.

        Each step is one /tokenize round-trip over the full 245K-token body. The obvious
        "pop one sentence until it fits" loop costs one such call per sentence removed --
        hundreds of them -- which turns prompt building into the slowest part of a run whose
        whole point is a 10-minute burst.
        """
        sentences = [x for x in tail.split(". ") if x]
        lo, hi = 0, len(sentences)
        if count(render(sentences[:hi])) <= target_tokens:
            return render(sentences[:hi])
        while lo < hi:
            mid = (lo + hi) // 2
            if count(render(sentences[:mid])) <= target_tokens:
                lo = mid + 1
            else:
                hi = mid
        keep = max(1, lo - 1)
        out = render(sentences[:keep])
        for _ in range(40):                      # bounded: never spin on a tokenizer quirk
            if count(out + " " + FILLER_SENTENCE) > target_tokens:
                break
            out = out + " " + FILLER_SENTENCE
        return out

    FILLER_SENTENCE = ("Slate rivers cross quiet valleys while copper clocks mark patient "
                       "hours. This is ordinary benchmark filler with no instructions.")
    return head + "\n" + question + "\n" + trim(rest)


def cache_counters(base_root: str) -> dict[str, float]:
    """Cumulative prefix-cache hit/query counters, so a leg can prove it stayed cold."""
    try:
        with urllib.request.urlopen(base_root.rstrip("/") + "/metrics", timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    want = {"vllm:prefix_cache_queries_total", "vllm:prefix_cache_hits_total",
            "vllm:external_prefix_cache_hits_total"}
    agg: dict[str, float] = {}
    for line in body.splitlines():
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name in want:
            try:
                agg[name] = agg.get(name, 0.0) + float(line.rsplit(" ", 1)[1])
            except ValueError:
                pass
    return agg


def cmd_phase(args) -> int:
    base_v1 = args.base_url
    base_root = base_v1[: -len("/v1")] if base_v1.rstrip("/").endswith("/v1") else base_v1
    out_dir = args.out_dir; out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag
    sets = load_or_build_prompts(base_v1, args.model, args.depth, args.concurrency,
                                 out_dir, args.reuse_prompts, args.question, args.approx,
                                 args.question_end)
    ref_prompts, burst_prompts = sets["reference"], sets["burst"]
    psha = sets["sha"]
    print(f"[{tag}] prompts sha={psha} depth={args.depth} n={args.concurrency}", flush=True)

    solo = []
    if not args.skip_solo:
        for i, p in enumerate(ref_prompts):
            solo.append(completion(base_v1, p, args.model, args.output_tokens, args.seed,
                                   args.request_timeout))
            print(f"  solo[{i}] ok={solo[-1]['ok']} sha={solo[-1]['text_sha']} "
                  f"{solo[-1]['seconds']}s", flush=True)

    demand = args.concurrency * (args.depth + args.output_tokens)
    print(f"[{tag}] COLD concurrent burst of {args.concurrency}, ~{demand:,} resident tokens",
          flush=True)
    pre0 = preemptions(base_root)
    cnt0 = cache_counters(base_root)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        together = list(pool.map(lambda p: completion(base_v1, p, args.model, args.output_tokens,
                                                      args.seed, args.request_timeout),
                                 burst_prompts))
    wall = round(time.time() - t0, 2)
    pre1 = preemptions(base_root)
    cnt1 = cache_counters(base_root)
    evicted = (pre1 - pre0) if None not in (pre0, pre1) else None
    dq = cnt1.get("vllm:prefix_cache_queries_total", 0.0) - cnt0.get("vllm:prefix_cache_queries_total", 0.0)
    dh = (cnt1.get("vllm:prefix_cache_hits_total", 0.0)
          + cnt1.get("vllm:external_prefix_cache_hits_total", 0.0)
          - cnt0.get("vllm:prefix_cache_hits_total", 0.0)
          - cnt0.get("vllm:external_prefix_cache_hits_total", 0.0))
    hit_ratio = round(dh / dq, 4) if dq > 0 else None
    rate = round(demand / wall) if wall else None
    # 0.5 rather than 0: the last block of a prompt can legitimately miss/re-hit, and a
    # cold 4 x 245K burst on a fresh engine should be near 0.00. Above half means the burst
    # was mostly served from cache and therefore never stressed the pool.
    cache_defeat = hit_ratio is not None and hit_ratio > args.max_cache_hit_ratio
    if cache_defeat:
        print(f"  !! prefix-cache hit ratio {hit_ratio} over {dq:,.0f} queried tokens: this "
              f"burst was largely CACHED, so it did not exercise prefill or eviction", flush=True)
    print(f"  burst wall={wall}s ({rate} tok/s effective) evictions={evicted} "
          f"prefix_hit_ratio={hit_ratio}", flush=True)
    for i, r in enumerate(together):
        print(f"  burst[{i}] ok={r['ok']} sha={r['text_sha']} tokens={r['n_tokens']} "
              f"{r['seconds']}s", flush=True)

    emitted = [t for b in together for t in (b.get("sequence") or [])]
    distinct = len(set(emitted))
    if distinct < args.min_distinct_tokens:
        # Checked HERE, at the end of the leg, rather than only in compare() an hour later:
        # a leg whose four sequences emit 22 distinct tokens between them (and byte-identical
        # output for different prompts) cannot support any verdict about eviction, and finding
        # that out after three more boots wastes the run.
        print(f"[{tag}] ABORTED: output entropy {distinct} distinct tokens across "
              f"{len(emitted):,} emitted (< {args.min_distinct_tokens}). The continuation is a "
              f"repetition cycle, so cross-leg agreement would be guaranteed by construction. "
              f"Put the question at the END of the prompt (--question-end).", flush=True)
        receipt = {"schema": "qwen38-eviction-identity-phase-v3", "tag": tag,
                   "aborted_low_entropy": distinct}
        (out_dir / f"{tag}.json").write_text(json.dumps(receipt))
        return 3
    receipt = {"schema": "qwen38-eviction-identity-phase-v3", "tag": tag,
               "base_url": base_v1, "model": args.model, "depth": args.depth,
               "concurrency": args.concurrency, "output_tokens": args.output_tokens,
               "seed": args.seed, "prompts_sha": psha, "wall_seconds": wall,
               "preemptions": evicted, "effective_prefill_tokens_per_second": rate,
               "prefix_cache_hit_ratio": hit_ratio, "cache_defeated": cache_defeat,
               "output_distinct_tokens": distinct, "output_total_tokens": len(emitted),
               "solo_seconds": [r.get("seconds") for r in solo],
               "burst_seconds": [r.get("seconds") for r in together],
               "solo": [{k: r.get(k) for k in ("ok", "text_sha", "seq_sha", "n_tokens",
                                               "logprobs", "sequence", "sequence_kind",
                                               "error")} for r in solo],
               "burst": [{k: r.get(k) for k in ("ok", "text_sha", "seq_sha", "n_tokens",
                                                "logprobs", "sequence", "sequence_kind",
                                                "error")} for r in together],
               # NOTE: solo used the reference set and burst the burst set, so this drift is
               # reference-vs-burst (different prompts) -- contextual only, never a verdict.
               "solo_burst_drift": [drift(a["logprobs"], b["logprobs"])
                                    for a, b in zip(solo, together)]}
    (out_dir / f"{tag}.json").write_text(json.dumps(receipt))
    print(f"[{tag}] wrote {out_dir / (tag + '.json')}: evictions={evicted} wall={wall}s", flush=True)
    return 0


def cmd_compare(args) -> int:
    runs = {}
    for tag in args.tags:
        path = args.out_dir / f"{tag}.json"
        if not path.exists():
            print(f"MISSING: {path}", file=sys.stderr); return 2
        runs[tag] = json.loads(path.read_text())
    tags = [t for t in args.tags if t in runs]
    evicting = [t for t in tags if (runs[t].get("preemptions") or 0) > 0]
    clean = [t for t in tags if not (runs[t].get("preemptions") or 0) > 0]

    print(f"legs: {len(tags)}  eviction-free: {clean or 'none'}  evicting: {evicting or 'none'}")
    for t in tags:
        r = runs[t]
        print(f"  {t}: preemptions={r.get('preemptions')} wall={r.get('wall_seconds')}s "
              f"prompts_sha={r.get('prompts_sha')}")

    same_prompts = len({runs[t].get("prompts_sha") for t in tags}) == 1
    if not same_prompts:
        print("FAIL: the legs did not run the same prompts, so their outputs are not comparable.")
        return 2

    defeated = [t for t in tags if runs[t].get("cache_defeated")]
    if defeated:
        print(f"INCONCLUSIVE: leg(s) {defeated} were served from the prefix cache "
              f"(hit ratio "
              f"{[runs[t].get('prefix_cache_hit_ratio') for t in defeated]}), so no prefill and "
              f"no eviction could occur. Give each phase its own prompt set.")
        return 2
    if not evicting:
        slow = {t: runs[t].get("effective_prefill_tokens_per_second") for t in tags}
        print(f"INCONCLUSIVE: no leg evicted, so the eviction path was never exercised. "
              f"Effective prefill rate per leg: {slow}. If that is far above ~6k tok/s the "
              f"burst was not really prefilled; otherwise raise --depth/--concurrency or "
              f"lower the utilization (band map: docs/qualification.md).")
        return 2

    # The floor is run-to-run, not solo-vs-burst. Two boots at the SAME eviction-free
    # utilization, same cold prompts, are the null control: whatever difference they show is
    # ordinary cross-run variance on this engine (batching, FP8 KV, MTP accept paths) and has
    # nothing to do with eviction. Judging an evicted leg against a single clean leg instead
    # would call ordinary variance a correctness veto -- and comparing solo against burst
    # inside one leg, which an earlier version did, compares different prompts and controls
    # nothing at all.
    def pair_report(x: dict, y: dict) -> dict:
        div, lcp_min, n = [], None, 0
        for i, (a, b) in enumerate(zip(x["burst"], y["burst"])):
            ta, tb = a.get("sequence") or [], b.get("sequence") or []
            m = min(len(ta), len(tb)); n = max(n, m)
            k = 0
            while k < m and ta[k] == tb[k]:
                k += 1
            if ta != tb:
                div.append({"index": i, "lcp_tokens": k, "of": m})
            lcp_min = k if lcp_min is None else min(lcp_min, k)
        return {"sequences": len(x["burst"]), "divergent": div,
                "divergent_count": len(div), "min_lcp": lcp_min, "max_tokens": n}

    subject = evicting[-1]
    r = runs[subject]
    floor_pairs = []
    for i in range(len(clean)):
        for j in range(i + 1, len(clean)):
            rep = pair_report(runs[clean[i]], runs[clean[j]])
            floor_pairs.append({"legs": [clean[i], clean[j]], **rep})
            print(f"null control {clean[i]} vs {clean[j]}: {rep['divergent_count']}/"
                  f"{rep['sequences']} sequences diverged, earliest agreement break at "
                  f"token {rep['min_lcp']}")
    if not floor_pairs:
        print("NOTE: only one eviction-free leg, so there is no null control. Any divergence "
              "found below cannot be attributed to eviction rather than ordinary cross-run "
              "variance -- record it, do not act on it.")

    references = [t for t in tags if t != subject]
    subject_pairs = []
    for t in references:
        rep = pair_report(runs[t], r)
        subject_pairs.append({"legs": [t, subject], **rep})
        print(f"subject {t} vs {subject}: {rep['divergent_count']}/{rep['sequences']} "
              f"sequences diverged, earliest agreement break at token {rep['min_lcp']}")

    # An absent payload must never count as agreement. The absence of this check is what
    # let the previous run report PASS over four empty token lists.
    def present(r: dict) -> int:
        return sum(1 for o in r["burst"] if len(o.get("sequence") or []) >= args.min_tokens)
    kinds = sorted({o.get("sequence_kind") for t in tags for o in runs[t]["burst"]})
    coverage = {t: f"{present(runs[t])}/{len(runs[t]['burst'])}" for t in tags}
    ent = {}
    for t in tags:
        allv = [x for o in runs[t]["burst"] for x in (o.get("sequence") or [])]
        ent[t] = {"tokens": len(allv), "distinct": len(set(allv))}
    entropy_str = ", ".join(f"{k}={v['tokens']:,}tok/{v['distinct']}distinct"
                            for k, v in ent.items())
    print(f"output entropy per leg: {entropy_str}")
    low = [t for t, v in ent.items() if v["distinct"] < args.min_distinct_tokens]
    if low:
        print(f"INCONCLUSIVE: leg(s) {low} emitted fewer than {args.min_distinct_tokens} "
              f"distinct tokens -- the continuation is a repetition cycle, so agreement "
              f"between legs is close to guaranteed by construction and does not demonstrate "
              f"that eviction preserved anything. Re-run with --question.")
        return 2
    print(f"compared representation: {kinds}; sequences with >= {args.min_tokens} units: {coverage}")
    empty_legs = [t for t in tags if present(runs[t]) < len(runs[t]["burst"])]
    if empty_legs:
        print(f"INCONCLUSIVE: leg(s) {empty_legs} returned no comparable token stream "
              f"(representation {kinds}); refusing to score empty output as agreement.")
        return 2

    floor_div = sum(f["divergent_count"] for f in floor_pairs)
    floor_seqs = sum(f["sequences"] for f in floor_pairs)
    subj_div = min((sp["divergent_count"] for sp in subject_pairs), default=0)
    floor_rate = (floor_div / floor_seqs) if floor_seqs else None
    subj_rate = (subj_div / subject_pairs[0]["sequences"]) if subject_pairs else None

    report = {"subject_leg": subject, "reference_legs": references, "floor_legs": clean,
              "floor_pairs": floor_pairs, "subject_pairs": subject_pairs,
              "floor_divergence_rate": floor_rate, "subject_divergence_rate": subj_rate}
    (args.out_dir / "compare.json").write_text(json.dumps(report, indent=2))

    print(f"\nsubject leg {subject} evicted {r.get('preemptions'):g} times "
          f"({r.get('wall_seconds')}s wall), cold prefill at "
          f"{r.get('effective_prefill_tokens_per_second')} tok/s, prefix hit ratio "
          f"{r.get('prefix_cache_hit_ratio')}.")
    worst = min((sp for sp in subject_pairs if sp["divergent"]),
                key=lambda sp: sp["min_lcp"], default=None)
    detail = f"; earliest agreement break at token {worst['min_lcp']}" if worst else ""
    if subj_div and floor_rate is not None and subj_rate > floor_rate:
        print(f"RESULT: FAIL -- eviction diverges MORE than the null control: {subj_div} of "
              f"{subject_pairs[0]['sequences']} sequences vs {floor_div} of {floor_seqs} for two "
              f"identical eviction-free boots{detail}.")
        return 1
    if subj_div:
        print(f"RESULT: INCONCLUSIVE -- {subj_div} sequence(s) diverged from the clean leg, but "
              f"the eviction-free legs diverge from each other at the same or higher rate "
              f"({floor_div}/{floor_seqs}) with no eviction involved. This engine is not "
              f"run-to-run token-reproducible on this workload, so eviction cannot be "
              f"isolated as the cause{detail}.")
        return 2
    if not floor_pairs:
        print(f"RESULT: PASS (weak) -- every sequence agreed token-for-token with the clean leg "
              f"across {r.get('preemptions'):g} evictions, but there is no null control, so this "
              f"is one comparison rather than a controlled experiment.")
        return 0
    print(f"RESULT: PASS -- all {subject_pairs[0]['sequences']} sequences agreed token-for-token "
          f"across {r.get('preemptions'):g} evictions, and the null-control boots agreed with "
          f"each other too ({floor_div}/{floor_seqs} diverged). Eviction did not change answers "
          f"beyond this engine's ordinary run-to-run variance.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("phase", help="run one utilization leg")
    ph.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    ph.add_argument("--model", default="qwen38-exl3")
    ph.add_argument("--depth", type=int, default=245248)
    ph.add_argument("--concurrency", type=int, default=4)
    ph.add_argument("--output-tokens", type=int, default=96)
    ph.add_argument("--seed", type=int, default=1234)
    ph.add_argument("--request-timeout", type=float, default=3600.0)
    ph.add_argument("--out-dir", type=Path, required=True)
    ph.add_argument("--tag", required=True, help="leg label, usually the utilization")
    ph.add_argument("--reuse-prompts", action="store_true",
                    help="reuse prompts.json so legs on different engines are comparable")
    ph.add_argument("--approx", action="store_true",
                    help="build prompts locally at approximately the target depth instead of "
                         "iterating /tokenize to hit it exactly (eviction needs oversubscription, "
                         "not an exact count; this removes ~80 full-length tokenizations/prompt)")
    ph.add_argument("--question-end", action="store_true",
                    help="place the open-ended ask LAST, where generation continues from; a "
                         "question buried under 245K tokens of filler is not remembered")
    ph.add_argument("--min-distinct-tokens", type=int, default=64,
                    help="abort the leg if its output uses fewer distinct tokens than this")
    ph.add_argument("--question", action="store_true",
                    help="inject an open-ended ask after the header so the continuation is "
                         "high entropy; without it the model recites the filler in a period-23 "
                         "cycle and agreement is nearly guaranteed by construction")
    ph.add_argument("--min-tokens", type=int, default=8,
                    help="a sequence with fewer units than this is treated as no evidence")
    ph.add_argument("--skip-solo", action="store_true",
                    help="omit the reference-set solo runs (legs that only need the burst)")
    ph.add_argument("--max-cache-hit-ratio", type=float, default=0.5,
                    help="fail the leg if the burst hit this fraction of the prefix cache -- "
                         "a cached burst does no prefill and cannot evict")
    ph.set_defaults(fn=cmd_phase)

    cp = sub.add_parser("compare", help="compare recorded legs")
    cp.add_argument("--out-dir", type=Path, required=True)
    cp.add_argument("--min-distinct-tokens", type=int, default=64,
                    help="a leg whose combined output uses fewer distinct tokens than this is "
                         "a repetition cycle and cannot support a verdict")
    cp.add_argument("--min-tokens", type=int, default=8,
                    help="a sequence with fewer units than this is treated as no evidence; "
                         "compare() itself needs this -- it is the guard that stops an empty "
                         "payload from being scored as agreement")
    cp.add_argument("--tags", nargs="+", required=True)
    cp.set_defaults(fn=cmd_compare)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
