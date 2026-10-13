#!/usr/bin/env python3
"""Biomedical paper-analysis gate for checkpoint swaps (base K4.25 vs Swift
6.05bpw vs any requant under consideration).

Rationale: the vendor cards disagree on the base model's IFBench by 8 points
and none of them measure biomedical structured extraction, which is the
actual workload here. This suite probes exactly the failure modes that
paper analysis hits: verbatim numeric extraction into a strict schema,
claim-vs-evidence auditing, word-bounded summarization, number-scope
reasoning, and one domain-transfer extraction. Gold answers are grounded
in abstract text quoted below (PubMed efetch, curated 2026-10-13); every
validator is mechanical - no judge model.

Tasks are greedy-decoded single shots (temperature 0); reasoning is
stripped by the server's reasoning parser, validators see final content.

Usage: python3 scripts/test-biomed-analysis.py --base-url http://127.0.0.1:8001 \
        --model qwen38-exl3 --output benchmarks/<dir>/biomed.jsonl
"""
from __future__ import annotations
import argparse, json, re, statistics, sys, time, urllib.request, pathlib

ABSTRACTS = {
 "A": """J Genet Eng Biotechnol. 2024 Mar;22(1):100351. Emergence of carbapenem
resistant gram-negative pathogens with high rate of colistin resistance in Egypt:
A cross sectional study to assess resistance trends during the COVID-19 pandemic.
[...] The current study investigated the temporal phenotypic and genotypic
antimicrobial resistance (AMR) trends among multi-drug resistant and
carbapenem-resistant Klebsiella pneumoniae, Acinetobacter baumannii, and
Pseudomonas aeruginosa recovered from Egyptian clinical settings between 2020
and 2021. Bacterial identification and antimicrobial sensitivity of 111 clinical
isolates against a panel of antibiotics were performed. [...] An alarming rate
(98.2%) of these isolates were found to be phenotypically resistant to
carbapenem. Although 23.9 % K. pneumoniae isolates were phenotypically resistant
to colistin, no mobile colistin resistance (mcr) genes were detected. Among
carbapenem-resistant isolates, blaNDM and blaOXA-48-like were the most prevalent
genetic determinants and were significantly overrepresented among K. pneumoniae.
Furthermore, 84.78% of K. pneumoniae isolates co-produced these two carbapenemase
genes. The plasmid-mediated quinolone resistance genes (qnrS and qnrB) were
detected [...] Class 1 integron was detected in 82% of the bacterial isolates.
This study alarmingly reveals elevated resistance to last-resort antibiotics such
as carbapenems as well as colistin which impose a considerable burden in the
health care settings in Egypt. [...] PMID: 38494251""",
 "B": """BMC Med Genomics. 2023 Oct 16;16(1):247. Proteomic analysis of lysine
2-hydroxyisobutyryl in SLE reveals protein modification alteration in complement
and coagulation cascades and platelet activation Pathways. BACKGROUND:
Post-translational modifications (PTMs) are considered to be an important factor
in the pathogenesis of Systemic lupus erythematosus (SLE). [...] METHODS: Khib
levels in SLE patients and healthy controls were compared based on liquid
chromatography tandem mass spectrometry, then proteomic analysis was conducted.
RESULTS: Compared with healthy controls, Khib in SLE patients was up-regulated at
865 sites of 416 proteins and down-regulated at 630 sites of 349 proteins. [...]
Bioinformatics analysis showed that Complement and coagulation cascades and
Platelet activation in immune-related pathways were significantly enriched [...]
PMID: 37845672""",
 "C": """Reproduction. 2023;165(4):457-474. PCSK9 involves in the high-fat
diet-induced abnormal testicular function of male mice. [...] we utilized
wild-type C57BL/6 male mice and PCSK9 knockout mice with same background as
experimental subjects and alirocumab, a PCSK9 inhibitor, was used for treatment.
Results indicated that HFD induced higher PCSK9 expression in serum, liver, and
testes, and serum PCSK9 is negatively correlated with spermatogenesis, while both
PCSK9 inhibitor treatment and PCSK9 knockout methodologies ameliorated impaired
lipid metabolism and spermatogenesis in mice fed a HFD. This could be due to the
overexpression of PCSK9 induced by HFD leading to dyslipidemia, resulting in
testicular lipotoxicity, thus activating the Bcl-2-Bax-Caspase3 apoptosis
signaling pathway in testes, particularly in Leydig cells. [...] This finding
could provide innovative ideas for the diagnosis and treatment of male
infertility. PMID: 36745023""",
 "E": """Phys Chem Chem Phys. 2023. Incorporating crown ethers into a graphene
lattice [...] This study employed density functional theory calculations to
introduce a series of novel graphitic carbon oxides [...] Their calculated band
gaps range from 0.11 eV to 4.38 eV. Specifically, our analysis reveals that C6G-1,
characterized by its largest crown ether-like nanopore with six oxygen atoms,
holds potential as a material for photocatalytic water splitting. [...]
PMID: 38112567""",
}

def jget(text):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m: return None
    try: return json.loads(m.group(1))
    except Exception: return None

def num_close(got, want, tol=0.05):
    try: return abs(float(got) - float(want)) <= tol
    except Exception: return False

def V_json_exact(got):
    j = jget(got)
    if not j: return False, "no parseable JSON"
    need = {"n_isolates":111, "carbapenem_resistant_pct":98.2, "kp_colistin_resistant_pct":23.9,
            "mcr_detected":False, "class1_integron_pct":82, "kp_coproduction_pct":84.78}
    for k, v in need.items():
        if k not in j: return False, f"missing {k}"
        if isinstance(v, bool):
            if not (isinstance(j[k], bool) and j[k] == v): return False, f"{k}!={v}"
        elif not num_close(j[k], v): return False, f"{k}={j[k]!r} want {v}"
    genes = set(g.lower().replace("bla","") for g in j.get("top_carbapenemases", []) if isinstance(g, str))
    if not ({"ndm"} <= genes and any("oxa-48" in g for g in genes)): return False, f"genes {genes}"
    return True, "ok"

def V_json_omics(got):
    j = jget(got)
    if not j: return False, "no parseable JSON"
    need = {"up_sites":865, "up_proteins":416, "down_sites":630, "down_proteins":349}
    for k, v in need.items():
        if not (k in j and num_close(j[k], v, 0)): return False, f"{k} bad/missing"
    blob0 = json.dumps(j).lower()
    if not any(a in blob0 for a in ("pbmc", "mononuclear")): return False, "sample not PBMC"
    blob = json.dumps(j).lower()
    if not ("complement" in blob and "coagulation" in blob and "platelet" in blob): return False, "pathways missing"
    return True, "ok"

def V_json_materials(got):
    j = jget(got)
    if not j: return False, "no parseable JSON"
    if not num_close(j.get("band_gap_min_ev", -9), 0.11): return False, "gap min"
    if not num_close(j.get("band_gap_max_ev", -9), 4.38): return False, "gap max"
    if "c6g-1" not in str(j.get("best_candidate", "")).lower(): return False, "candidate"
    if not num_close(j.get("oxygen_atoms", -1), 6, 0): return False, "oxygen count"
    if "water" not in str(j.get("application", "")).lower(): return False, "application"
    return True, "ok"

def V_label(accept, keywords):
    def v(got):
        low = got.lower()
        head = low.strip().split("\n")[0]
        if not any(a in head for a in accept): return False, "verdict missing from first line"
        if keywords and not any(k in low for k in keywords): return False, "reasoning keywords missing"
        return True, "ok"
    return v

def V_summary(lo, hi, must):
    """must entries are '|'-separated alternatives; each must match at least one."""
    def v(got):
        words = len(got.split())
        if not lo <= words <= hi: return False, f"word count {words} outside [{lo},{hi}]"
        low = got.lower()
        missing = [m for m in must if not any(alt in low for alt in m.split("|"))]
        if missing: return False, f"missing terms {missing}"
        return True, "ok"
    return v

def V_contains(any_of, name=""):
    def v(got):
        low = got.lower()
        return (any(a in low for a in any_of), "ok" if any(a in low for a in any_of) else f"none of {any_of}")
    return v

def tasks():
    A, B, C, E = (ABSTRACTS[k] for k in "ABCE")
    return [
     dict(id="A1-extract", key="A", max_tokens=3072, validator=V_json_exact,
      prompt="Extract from this abstract into EXACTLY this JSON and nothing else:\n"
             '{"n_isolates": int, "carbapenem_resistant_pct": float, "kp_colistin_resistant_pct": float,'
             ' "mcr_detected": bool, "kp_coproduction_pct": float, "class1_integron_pct": number,'
             ' "top_carbapenemases": [strings]}\n\n' + A),
     dict(id="A2-claim", key="A", max_tokens=900, validator=V_label(["not supported","not_supported"], ["mcr"]),
      prompt="Claim under audit: \"The study detected mobile colistin resistance (mcr) genes in most"
             " colistin-resistant K. pneumoniae isolates.\"\nBased ONLY on the abstract, answer"
             " SUPPORTED or NOT_SUPPORTED as the first line, then one sentence of evidence.\n\n" + A),
     dict(id="A3-summarize", key="A", max_tokens=3072, validator=V_summary(60, 80, ["colistin", "carbapenem"]),
      prompt="Summarize this study for a clinician in EXACTLY 60-80 words, plain prose, no bullets.\n\n" + A),
     dict(id="A4-scope", key="A", max_tokens=500, validator=V_contains(["k. pneumoniae", "k pneumoniae", "klebsiella"]),
      prompt="The abstract reports 23.9% colistin phenotypic resistance and 98.2% carbapenem resistance."
             " Does the 23.9% figure describe all isolates in the study, or a specific subset?"
             " Answer in one sentence naming the exact subset.\n\n" + A),
     dict(id="B1-extract", key="B", max_tokens=6144, validator=V_json_omics,
      prompt="Extract into EXACTLY this JSON and nothing else:\n"
             '{"up_sites": int, "up_proteins": int, "down_sites": int, "down_proteins": int,'
             ' "sample": string, "enriched_pathways": [strings]}\n'
             'Here "sample" means the biological specimen analyzed (a cell type or tissue),'
             ' not the disease or the patients.\n\n' + B),
     dict(id="B2-claim", key="B", max_tokens=900,
      validator=V_label(["not supported","not_supported"], ["up-regulated","up regulated","upregul","both","mixed"]),
      prompt="Claim under audit: \"Khib modification was uniformly down-regulated in SLE patients.\"\n"
             "Based ONLY on the abstract, answer SUPPORTED or NOT_SUPPORTED as the first line, then one"
             " sentence of evidence.\n\n" + B),
     dict(id="B3-design", key="B", max_tokens=400,
      validator=V_contains(["proteomic","case-control","case control","comparative","mass spectrometry","observational"]),
      prompt="In at most 8 words, name the study design of this work (e.g. trial type, cohort type, or"
             " assay-level design).\n\n" + B),
     dict(id="B4-scope", key="B", max_tokens=500, validator=V_contains(["protein"]),
      prompt="In the abstract, does the number 416 refer to modification sites or to proteins?"
             " One sentence answer.\n\n" + B),
     dict(id="C1-extract", key="C", max_tokens=3072,
      validator=lambda g: (True,"ok") if (lambda j: j and "c57" in json.dumps(j).lower()
        and "knockout" in json.dumps(j).lower() and "alirocumab" in json.dumps(j).lower()
        and "caspase" in json.dumps(j).lower().replace("-","") and "leydig" in json.dumps(j).lower())(jget(g)) else (False,"fields missing"),
      prompt="Extract into EXACTLY this JSON and nothing else:\n"
             '{"animal_model": string, "genetic_intervention": string, "drug_intervention": string,'
             ' "apoptosis_pathway": string, "key_cell_type": string}\n\n' + C),
     dict(id="C2-claim", key="C", max_tokens=900,
      validator=V_label(["not supported","not_supported"], ["inhibit","knockout","knock-out","amelior","reduc"]),
      prompt="Claim under audit: \"Raising PCSK9 levels ameliorates HFD-induced spermatogenesis failure.\"\n"
             "Based ONLY on the abstract, answer SUPPORTED or NOT_SUPPORTED as the first line, then one"
             " sentence of evidence.\n\n" + C),
     dict(id="C3-limit", key="C", max_tokens=500, validator=V_label(["no"], ["mouse","mice","animal"]),
      prompt="Does this study directly demonstrate that alirocumab treats human male infertility?"
             " Answer YES or NO as the first line, then one sentence why.\n\n" + C),
     dict(id="E1-transfer", key="E", max_tokens=3072, validator=V_json_materials,
      prompt="Extract into EXACTLY this JSON and nothing else:\n"
             '{"band_gap_min_ev": float, "band_gap_max_ev": float, "best_candidate": string,'
             ' "oxygen_atoms": int, "application": string}\n\n' + E),
     dict(id="D1-cross", key=None, max_tokens=6144,
      validator=V_summary(15, 30, ["resist", "lupus|sle", "infertil|spermatogen|testic|sperm"]),
      prompt="Three paper excerpts follow. In 15-30 words TOTAL, name the one health condition"
             " each paper is about, in order, separated by semicolons.\n\n"
             "PAPER 1 (excerpt): " + A[:700] + "\n\nPAPER 2 (excerpt): " + B[:700]
             + "\n\nPAPER 3 (excerpt): " + C[:900]),
    ]

def ask(base_url, model, prompt, max_tokens):
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"] or "", d.get("usage", {})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--model", required=True)
    ap.add_argument("--output", type=pathlib.Path, required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default=None, help="comma-separated task ids to run (receipts are appended to a side file)")
    a = ap.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    results, passed = [], 0
    todo = tasks()
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        todo = [t for t in todo if t["id"] in keep]
        a.output = a.output.with_suffix(a.output.suffix + f".only-{'-'.join(sorted(keep))}")
    with open(a.output, "w") as out:
        for t in todo:
            per_run = []
            for run in range(a.runs):
                t0 = time.time()
                content, usage = ask(a.base_url, a.model, t["prompt"], t["max_tokens"])
                ok, why = t["validator"](content)
                ct = usage.get("completion_tokens") or 0
                rec = {"id": t["id"], "run": run, "pass": bool(ok), "why": why,
                       "seconds": round(time.time() - t0, 1), "usage": usage,
                       "truncated": ct >= t["max_tokens"] and not content.strip(),
                       "content_head": content[:400]}
                per_run.append(rec); out.write(json.dumps(rec) + "\n"); out.flush()
                print(f"{t['id']} run {run + 1}: {'PASS' if ok else 'FAIL'} ({why})", flush=True)
            if any(r["pass"] for r in per_run): passed += 1
    print(f"\n{a.model}: {passed}/{len(todo)} tasks passed (majority of runs); receipts: {a.output}")
    return 0 if passed == len(tasks()) else 1

if __name__ == "__main__":
    sys.exit(main())
