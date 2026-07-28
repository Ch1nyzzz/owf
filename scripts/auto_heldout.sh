#!/usr/bin/env bash
# Watch the protocol-v3 optimization runs; as each finishes, pick its champion
# (best score, leanest among ties) and launch the held-out test automatically.
# Also runs the realmath seed baseline test (never measured) alongside the first
# realmath champion test. Detached via setsid so it needs no operator.
#
# Log: runs/auto_heldout.log

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
LOG=runs/auto_heldout.log
echo "[$(date +%F' '%T)] auto_heldout watcher started" >> "$LOG"

run_test() {  # kind(workflow|agent) domain champion_path out_dir
  local kind=$1 domain=$2 champ=$3 out=$4
  if [ -f "$out/report.json" ]; then
    echo "[$(date +%F' '%T)] $out already has a report — skip" >> "$LOG"
    return
  fi
  local flag=--workflow
  [ "$kind" = "agent" ] && flag=--agent-file
  mkdir -p "$out"
  echo "[$(date +%F' '%T)] launching test: $domain $kind $champ -> $out" >> "$LOG"
  setsid nohup bash -c "set -a; source .env; set +a; export PYTHONPATH=bench; \
    exec python3 -u bench/owf_bench/core/runner.py --domain $domain --subset test \
    --workers 24 --max-tokens 600000 --max-wallclock-sec 1800 \
    $flag '$champ' --out '$out'" >> "$out/eval.log" 2>&1 < /dev/null &
}

champion_workflow() {  # state.json path -> champion workflow path
  python3 -c "
import json,sys
s=json.load(open('$1'))
p=min(s['frontier'], key=lambda p:(-p['score'], p['tokens']))
print(p['workflow'])"
}

champion_meta() {  # frontier_val.json path -> champion agent path
  python3 -c "
import json
f=json.load(open('$1'))
p=min(f['pareto'], key=lambda p:(-p['score'], p['tokens']))
print(p['workflow'])"
}

watch_main() {  # run_root domain test_out
  local run=$1 domain=$2 out=$3
  while pgrep -f "optimize\.py.*$run" >/dev/null; do sleep 120; done
  local champ
  champ=$(champion_workflow "runs/$run/state.json" 2>>"$LOG") || { echo "[$(date +%F' '%T)] $run: no state.json champion" >> "$LOG"; return; }
  echo "[$(date +%F' '%T)] $run finished; champion: $champ" >> "$LOG"
  # bcplus seed already tested (runs/test_bcplus_seed); skip a seed champion there
  if [ "$domain" = bcplus ] && [[ "$champ" == *seed_parity.js ]]; then
    echo "[$(date +%F' '%T)] $run champion is the seed — already tested, skip" >> "$LOG"
    return
  fi
  run_test workflow "$domain" "$champ" "$out"
}

watch_meta() {  # run_root domain test_out
  local run=$1 domain=$2 out=$3
  while pgrep -f "meta_loop\.py.*$run" >/dev/null; do sleep 120; done
  local champ
  champ=$(champion_meta "runs/$run/frontier_val.json" 2>>"$LOG") || { echo "[$(date +%F' '%T)] $run: no frontier champion" >> "$LOG"; return; }
  echo "[$(date +%F' '%T)] $run finished; champion: $champ" >> "$LOG"
  if [ "$domain" = bcplus ] && [[ "$champ" == *baseline_bcplus.mjs ]]; then
    echo "[$(date +%F' '%T)] $run champion is the seed — already tested, skip" >> "$LOG"
    return
  fi
  run_test agent "$domain" "$champ" "$out"
}

watch_realmath_seed() {  # fire the never-measured realmath seed test once realmath main finishes
  while pgrep -f "optimize\.py.*opt_realmath_v8" >/dev/null; do sleep 120; done
  run_test workflow realmath workflows/realmath/seed_parity.js runs/test_realmath_seed
}

watch_main opt_realmath_v8 realmath runs/test_realmath_mainarm_v8 &
watch_main opt_bcplus_v7 bcplus runs/test_bcplus_mainarm_v7 &
watch_meta metaharness_realmath_v3 realmath runs/test_realmath_meta_v3 &
watch_meta metaharness_bcplus_v3 bcplus runs/test_bcplus_meta_v3 &
watch_realmath_seed &
wait
echo "[$(date +%F' '%T)] all watchers done; tests launched" >> "$LOG"
