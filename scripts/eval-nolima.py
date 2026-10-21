#!/usr/bin/env python3
"""Run NoLiMa (zero lexical-overlap associative retrieval) against our endpoint.

Replicates the adobe-research/NoLiMa released convention: needle sentences with
{CHAR} plus world/commonsense one- and two-hop questions, the released task
template, and gold = the needle's character name.  Deviations, recorded here
and in the receipts: placement snaps to the nearest sentence boundary (upstream
uses raw token offsets -- sub-1% depth difference), scoring reports both the
paper's contains-gold convention and strict EM, the base anchor is the 1K cell
rather than the max over {250,500,1K}, and depth intervals are 5 rather than
the upstream 35 (their long-context protocol also reduces placements).
Haystacks are the released shuffled PG-style books, sliced to the target token
count via the server tokenizer; the actual prompt_tokens come from usage.
"""
import argparse
import hashlib
import json
import random
from pathlib import Path
import urllib.request


def post_json(base_url: str, path: str, payload: dict, timeout: float = 1800) -> dict:
    request = urllib.request.Request(base_url.rstrip("/") + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fill(template: str, char: str, input_args) -> str:
    text = template.replace("{CHAR}", char)
    for i, arg in enumerate(input_args, start=1):
        text = text.replace("{" + str(i) + "}", arg)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--needle-set", type=Path, required=True)
    parser.add_argument("--haystack-dir", type=Path, required=True)
    parser.add_argument("--lengths", type=int, nargs="+",
                        default=[1024, 8192, 16384, 32768, 65536, 131072])
    parser.add_argument("--depths", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/").removesuffix("/v1")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    experiments = json.loads(args.needle_set.read_text())
    items = []
    for exp in experiments:
        for qtype, qtemplate in exp["questions"].items():
            for test_id, test in exp["tests"].items():
                items.append((exp, qtype, qtemplate, test_id, test["input_args"]))

    books = sorted(args.haystack_dir.glob("rand_book_*.txt"))
    if len(books) != 5:
        parser.error(f"expected 5 rand_book_*.txt files, found {len(books)}")
    book_ids = []
    for book in books:
        token_ids = post_json(base, "/tokenize",
                              {"model": args.model, "prompt": book.read_text()})["tokens"]
        book_ids.append(token_ids)

    def place(book_index: int, depth: float, context_length: int, needle: str):
        ids = book_ids[book_index]
        needle_ids = post_json(base, "/tokenize", {"model": args.model, "prompt": " " + needle})["tokens"]
        end = min(len(ids), context_length + len(needle_ids) + 64)
        cut = int(depth * (context_length - len(needle_ids) - 16))
        prefix = post_json(base, "/detokenize", {"model": args.model, "tokens": ids[:cut]})["prompt"]
        suffix = post_json(base, "/detokenize", {"model": args.model, "tokens": ids[cut:end]})["prompt"]
        snap = prefix.rfind(". ")
        if snap > len(prefix) // 2:
            prefix, suffix = prefix[:snap + 1], prefix[snap + 1:] + suffix
        return f"{prefix} {needle} {suffix}".strip()

    def chat(system: str, user: str):
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        return post_json(base, "/v1/chat/completions",
                         {"model": args.model, "messages": messages, "temperature": 0,
                          "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False}})

    with args.output.open("x") as output:
        def emit(row):
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()

        emit({"record": "meta", "schema": "qwen38-nolima-v1",
              "args": {k: str(v) for k, v in vars(args).items()},
              "items": len(items),
              "method": "released needle set + task template; gold=selected character; "
                        "contains-primary plus strict EM; sentence-snapped token-depth placement"})
        for length in args.lengths:
            cont = em = answered = skipped = 0
            ttfts = []
            for exp, qtype, qtemplate, test_id, input_args in items:
                rng = random.Random(hashlib.sha256(
                    f"{args.seed}|{exp['id']}|{qtype}|{test_id}".encode()).hexdigest())
                char = rng.choice(exp["character_set"])
                needle = fill(exp["needle"], char, input_args)
                question = fill(qtemplate, char, input_args)
                book_index = rng.randrange(5)
                for i in range(args.depths):
                    depth = (i + 0.5) / args.depths
                    text = place(book_index, depth, length, needle)
                    user = exp["task_template"].format(haystack=text, question=question)
                    try:
                        response = chat(exp.get("system_prompt", ""), user)
                    except Exception as exc:  # noqa: BLE001 - count window rejects
                        if "400" in str(exc):
                            skipped += 1
                            continue
                        raise
                    content = response["choices"][0]["message"]["content"].strip()
                    hit = char.lower() in content.lower()
                    cont += hit
                    em += content == char
                    answered += 1
                    emit({"record": "measurement", "exp": exp["id"], "reasoning": exp["reasoning_type"],
                          "question_type": qtype, "test": test_id, "length": length,
                          "depth": round(depth, 3), "gold": char, "response": content[:120],
                          "contains": hit, "em": content == char, "usage": response["usage"]})
            cell = {"record": "cell", "length": length, "answered": answered,
                    "over_window_skipped": skipped,
                    "contains_accuracy": round(cont / answered, 4),
                    "em_accuracy": round(em / answered, 4)}
            emit(cell)
            print(f"length={length}: contains={cell['contains_accuracy']} em={cell['em_accuracy']} "
                  f"answered={answered} skipped={skipped}", flush=True)


if __name__ == "__main__":
    main()
