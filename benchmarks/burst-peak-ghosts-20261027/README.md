ghost 1 (ghost-1-wrong-card-peak.tsv): nvidia-smi with no --id appends one line PER GPU, so peak_used_MiB
  was 95,986 = the grid engine's card 0, while the probe's own engine sat on card 2 at 92,076.
  Every phase read delta 0 against a neighbour's footprint. Caught only because nvidia-smi
  was queried per-index during an audit, three minutes into a run that looked healthy.

ghost 2 (ghost-2-*): the same script was edited while bash was executing it. Result: a duplicated
  'vision' phase row, and driver.log holding
    line 97: ((: 5790,: arithmetic syntax error: operand expected
  i.e. the guard meant to catch ghost 1 was itself broken twice over -- 'read' consumes one line
  so tr ',' '\n' pushed total onto line 2 and left it empty, and the arithmetic silently errored
  instead of failing the run. A check that cannot fail is worse than no check.

Both fixed; the third attempt (burst-peak-20261027c) is scoped to one index, prints its own
baseline-vs-free consistency line, and its guard has been proven in all three directions:
  right card -> passes; neighbour's baseline -> fires; other card's free -> fires.
