#!/system/bin/sh
# Runs inside the Android (termux-docker) container with most of the Termux prefix
# hidden, so only what the app ships and what Android provides is available.
# (The container's own toybox needs Termux libs, so only shell builtins are used here.)
# Pass 1 uses LD_LIBRARY_PATH like the app on arm64 phones; pass 2 relies on the
# $ORIGIN RUNPATHs alone (what x86 devices with ARM translation get).
S=/data/data/com.termux/files/home/sim
R=$S/files/hermes-agent
RC=0
first() { read -r line; echo "$1 -> $line"; while read -r _; do :; done; }
dump() { while IFS= read -r line; do echo "$line"; done < "$1"; }
PORT=9119
for mode in ld-path runpath-only; do
  if [ "$mode" = runpath-only ]; then export NO_LD_LIBRARY_PATH=1; PORT=9120; fi
  export SMOKE_PORT=$PORT
  echo "===== pass: $mode"
  if ! "$S/files/run.sh" --py "$S/smoke_runtime_fixes.py" "$R"; then
    RC=1
  fi
  if [ "$mode" = ld-path ]; then
    if "$S/files/run.sh" --py "$R/webui/android_toolchain.py" \
        --catalog "$R/webui/toolchain/catalog.json" \
        --root "$HERMES_ANDROID_TOOLCHAIN_ROOT" install-closure \
        bash git curl wget tar unzip coreutils findutils > "$S/toolchain-install.json"; then
      grep -q '"bash"' "$S/toolchain-install.json" && echo TOOLCHAIN_INSTALL_OK || RC=1
    else
      echo "== toolchain install log"
      dump "$S/toolchain-install.json"
      RC=1
    fi
  fi
  # Refreshes from installed state.json/facts.json, matching RuntimeEnv.kt;
  # these commands must resolve the installed toolchain, not the payload.
  check_exec() {
    label="$1"
    shift
    output=$("$S/files/run.sh" --exec "$@" 2>&1)
    status=$?
    printf '%s\n' "$output" | first "$label"
    if [ "$status" -ne 0 ]; then RC=1; fi
  }
  check_exec installed-bash bash --version
  check_exec python python --version
  check_exec node node --version
  check_exec npm npm --version
  check_exec rg rg --version
  check_exec ffmpeg ffmpeg -version
  if "$S/files/run.sh" --py "$R/webui/android_toolchain.py" \
      --catalog "$R/webui/toolchain/catalog.json" \
      --root "$HERMES_ANDROID_TOOLCHAIN_ROOT" env --shell > "$S/toolchain-env-$mode.sh"; then
    grep -q 'HERMES_ANDROID_TOOLCHAIN_PATH=' "$S/toolchain-env-$mode.sh" || RC=1
  else
    RC=1
  fi
  if "$S/files/run.sh" --py "$R/webui/android_toolchain.py" \
      --catalog "$R/webui/toolchain/catalog.json" \
      --root "$HERMES_ANDROID_TOOLCHAIN_ROOT" inventory > "$S/toolchain-$mode.json"; then
    grep -q '"schema_version": 1' "$S/toolchain-$mode.json" && \
      grep -q '"bash"' "$S/toolchain-$mode.json" && echo TOOLCHAIN_OK || RC=1
  else
    RC=1
  fi
  "$S/files/run.sh" > "$S/server-$mode.log" 2>&1 &
  PID=$!
  if ! "$S/files/run.sh" --py "$S/smoke_webui.py" "$PORT" "$R"; then
    RC=1
    echo "== server log ($mode)"
    dump "$S/server-$mode.log"
  fi
  while IFS= read -r line; do
    case "$line" in *"out of sync"*|*Traceback*) echo "server: $line" ;; esac
  done < "$S/server-$mode.log"
  kill "$PID" 2>/dev/null
done
exit $RC
