#!/usr/bin/env bash
# Adaptive power governor (owner policy 2026-10-15):
#  - inference window = engine API ready, GPU0 util avg >=70%, VRAM stable (excludes boot streaming)
#  - replay climb (GPU0 counter) in a qualified window -> cap -10W (floor 300), cooldown 1 window
#  - no climb in a qualified window -> cap +10W (ceil 450)
#  - writes /tmp/power-cap (consumed by power-guard.sh); set /tmp/power-govern=off to freeze.
set -u
CAP=$(cat /tmp/power-cap 2>/dev/null || echo 375); echo "$CAP" > /tmp/power-cap
replay(){ timeout 20 nvidia-smi -q 2>/dev/null | grep -aA0 "Replays Since Reset" | awk -F: 'NR==1{gsub(/ /,"",$2); print $2+0}'; }
util(){ nvidia-smi -i 0 --query-gpu=utilization.gpu --format=csv,noheader,nounits; }
mem(){ nvidia-smi -i 0 --query-gpu=memory.used --format=csv,noheader,nounits; }
ready(){ curl -s --max-time 3 http://127.0.0.1:8001/v1/models >/dev/null; }
log(){ echo "[$(date +%H:%M)] $*" >> ~/.cache/huggingface/power-gov.log; }
log "governor start cap=${CAP}W"
while true; do
  [[ "$(cat /tmp/power-govern 2>/dev/null)" == off ]] && { sleep 60; continue; }
  r0=$(replay); u_sum=0; u_n=0; stable=1; prev=$(mem); qualified=0
  for s in $(seq 1 20); do
    sleep 15
    u=$(util); u_sum=$((u_sum+u)); u_n=$((u_n+1))
    m=$(mem); (( m > prev + 500 || m < prev - 500 )) && stable=0; prev=$m
  done
  [[ "$(cat /tmp/power-govern 2>/dev/null)" == off ]] && continue
  uavg=$(( u_sum / (u_n>0?u_n:1) ))
  if ready && (( uavg >= 70 )) && (( stable == 1 )); then
    r1=$(replay); d=$(( r1 - r0 ))
    if (( d > 0 )); then
      CAP=$(( CAP - 10 )); (( CAP < 300 )) && CAP=300
      echo "$CAP" > /tmp/power-cap; log "inference window u=${uavg}% replay +${d} -> cap ${CAP}W (cooldown)"
      sleep 300
    else
      CAP=$(( CAP + 10 )); (( CAP > 450 )) && CAP=450
      echo "$CAP" > /tmp/power-cap; log "inference window u=${uavg}% replay +0 -> cap ${CAP}W"
    fi
  else
    log "window skipped (u=${uavg}% stable=${stable} ready=$(ready && echo y || echo n)) cap stays ${CAP}W"
  fi
done
