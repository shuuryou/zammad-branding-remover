#!/usr/bin/env bash
#
# removebranding.sh - replace Zammad logos with the company logo and remove Zammad's
# self-promotion ("Powered by", translators-wanted ads, careers banner in the browser console).
# Re-run it after every Zammad update.
#
# Usage:
#   removebranding.sh --check          plan + validate + audit only; changes NOTHING (safe any time)
#   removebranding.sh                  full run: patch, rebuild assets, restart Zammad
#   removebranding.sh --no-rebuild     patch sources only (no asset rebuild / restart)
#   removebranding.sh --no-restart     patch + rebuild, but do not restart Zammad
#   removebranding.sh --partial        apply what can be patched even if some rule fails
#   removebranding.sh --rollback DIR   undo a previous run using its backup dir
#
# How it works: data/patch_branding.py does all the editing (content-anchored, idempotent,
# validated before it writes anything, see its header). This wrapper adds backups, a lock,
# safe handling of the compiled assets and an automatic rollback if anything fails after
# the first file was changed. Branding assets live in data/ (logo-wordmark.svg is the single
# source of truth: favicon, app icons, error-page art and all logo files are generated from it).
#
set -Eeuo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ZAMMAD_ROOT=${ZAMMAD_ROOT:-/opt/zammad}
BACKUP_ROOT=${BACKUP_ROOT:-/var/backups/zammad-branding}
KEEP_BACKUPS=${KEEP_BACKUPS:-5}
ENGINE="$SCRIPT_DIR/data/patch_branding.py"

MODE=run
REBUILD=1
RESTART=1
PARTIAL=()
ROLLBACK_DIR=

log()  { printf '[%s] %s\n' "$(date +%T)" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --check)       MODE=check ;;
        --no-rebuild)  REBUILD=0 ;;
        --no-restart)  RESTART=0 ;;
        --partial)     PARTIAL=(--partial) ;;
        --rollback)    MODE=rollback; shift; ROLLBACK_DIR=${1:-}; [[ -n $ROLLBACK_DIR ]] || die "--rollback needs a backup dir" ;;
        -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)             die "unknown option: $1" ;;
    esac
    shift
done

engine() { python3 "$ENGINE" --root "$ZAMMAD_ROOT" --data "$SCRIPT_DIR/data" "$@"; }

# Compiled-asset state that the rebuild rewrites; saved so a failed run can be undone.
STATE_GLOBS=(
    'public/assets/.sprockets-manifest-*.json'
    'public/assets/frontend/vite/.vite/manifest.json'
    'public/assets/frontend/vite/.vite/manifest-assets.json'
    'public/assets/frontend/vite/sw.js'
    'tmp/cache/vite/last-build-production.json'
)

BK=
STAGE=init
ROLLBACK_ARMED=0
QUARANTINED=0
RESTARTED=0

list_top_assets() {
    find "$ZAMMAD_ROOT/public/assets" -maxdepth 1 -type f \( -name '*.js' -o -name '*.css' -o -name '*.gz' \) -printf '%f\n' | sort
}

# Every step reports failure explicitly (callers use it inside `if`, where `set -e` is off).
rollback() {
    local dir=$1 f
    log "ROLLING BACK using $dir"
    engine --restore "$dir" || return 1
    if [[ -d $dir/state ]]; then
        cp -a "$dir/state/." "$ZAMMAD_ROOT/" || return 1
    fi
    if [[ -d $dir/assets-quarantine ]]; then
        find "$dir/assets-quarantine" -type f -exec mv -f -t "$ZAMMAD_ROOT/public/assets" {} + || return 1
    fi
    if [[ -f $dir/assets-before.txt ]]; then
        # drop top-level assets created by the failed build
        while read -r f; do
            rm -f -- "$ZAMMAD_ROOT/public/assets/$f" || return 1
        done < <(comm -13 "$dir/assets-before.txt" <(list_top_assets))
    fi
    if ((RESTARTED)); then
        systemctl restart zammad || return 1
    fi
    log "ROLLED BACK. Zammad is back on its pre-run files."
}

on_error() {
    local rc=$? line=${BASH_LINENO[0]:-?}
    trap - ERR INT TERM
    printf '\nFAILED during stage "%s" (exit %s, line %s)\n' "$STAGE" "$rc" "$line" >&2
    if ((ROLLBACK_ARMED)) && [[ -n $BK ]]; then
        if ! ( rollback "$BK" ); then
            printf '\n*** ROLLBACK FAILED. STOP. Do not run anything else; restore the VM from Veeam. ***\n' >&2
            printf '*** Backup of originals: %s ***\n' "$BK" >&2
            exit 99
        fi
    else
        printf 'Nothing had been changed yet.\n' >&2
    fi
    exit "${rc:-1}"
}
trap on_error ERR INT TERM

# --------------------------------------------------------------------------- preflight
[[ $EUID -eq 0 ]] || die "must run as root"
command -v python3 >/dev/null || die "python3 not found"
[[ -f $ZAMMAD_ROOT/VERSION ]] || die "$ZAMMAD_ROOT does not look like Zammad"
[[ -f $ENGINE ]] || die "missing $ENGINE"
exec 9>/var/lock/removebranding.lock
flock -n 9 || die "another removebranding.sh is already running"

if [[ $MODE == rollback ]]; then
    [[ -d $ROLLBACK_DIR ]] || die "$ROLLBACK_DIR is not a backup dir of this script"
    trap - ERR INT TERM
    RESTARTED=1
    rollback "$ROLLBACK_DIR" || die "rollback failed - restore from Veeam"
    exit 0
fi

log "Zammad $(cat "$ZAMMAD_ROOT/VERSION")"

if [[ $MODE == check ]]; then
    STAGE="check"
    engine --dry-run
    exit 0
fi

avail_kb=$(df --output=avail -k "$ZAMMAD_ROOT" | tail -1 | tr -d ' ')
((avail_kb > 2*1024*1024)) || die "less than 2 GB free on $ZAMMAD_ROOT"
if [[ $REBUILD == 1 ]]; then
    mem_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    ((mem_mb > 1500)) || log "WARNING: only ${mem_mb} MB RAM available - the Vue (vite) build is memory hungry"
fi

TS=$(date +%Y%m%d-%H%M%S)
BK="$BACKUP_ROOT/$TS"
mkdir -p "$BK"
log "Backup directory: $BK"

# --------------------------------------------------------------------------- patch sources
STAGE="patching sources (nothing is written unless every rule validates)"
echo
log "Patching sources, logos, icons and error pages..."
trap - ERR            # a non-zero exit of the engine is handled explicitly below
set +e
engine --backup-dir "$BK" "${PARTIAL[@]}" 2>&1 | tee "$BK/patch.log"
rc=${PIPESTATUS[0]}
set -e
trap on_error ERR
if ((rc != 0)); then
    if ((${#PARTIAL[@]} == 0)); then
        # strict mode: the engine wrote nothing
        die "patch engine reported problems (exit $rc) - NOTHING was changed; see above"
    fi
    log "WARNING: --partial given, continuing although some rules failed (see above)"
fi
ROLLBACK_ARMED=1

if [[ $REBUILD == 0 ]]; then
    log "Sources patched; compiled assets NOT rebuilt (--no-rebuild). The change is not visible until:"
    log "    zammad run rake assets:precompile && systemctl restart zammad"
    exit 0
fi

# --------------------------------------------------------------------------- rebuild assets
STAGE="saving compiled-asset state"
(
    cd "$ZAMMAD_ROOT"
    mkdir -p "$BK/state"
    shopt -s nullglob
    for g in "${STATE_GLOBS[@]}"; do
        for f in $g; do cp -a --parents "$f" "$BK/state/"; done
    done
)
list_top_assets > "$BK/assets-before.txt"

# Stale compiled assets are MOVED into the backup dir (not deleted), so a failed build can be undone.
STAGE="quarantining stale compiled assets"
mkdir -p "$BK/assets-quarantine"
log "Moving stale compiled assets aside..."
find "$ZAMMAD_ROOT/public/assets" -maxdepth 1 -type f \( -name '*.js' -o -name '*.css' -o -name '*.gz' \) \
    -exec mv -t "$BK/assets-quarantine" {} +
QUARANTINED=1

STAGE="clearing cache"
log "Clearing cache..."
zammad run rails r "Rails.cache.clear"

STAGE="rake assets:precompile (legacy bundle + Vue/vite build; this takes a while)"
log "Recompiling assets (log: $BK/precompile.log)..."
# The dependency-install hook (pnpm install over the network) is skipped when vite is already there.
if [[ -f $ZAMMAD_ROOT/node_modules/vite/bin/vite.js ]]; then
    export VITE_RUBY_SKIP_ASSETS_PRECOMPILE_INSTALL=true
fi
zammad run rake assets:precompile 2>&1 | tee "$BK/precompile.log"
compgen -G "$ZAMMAD_ROOT/public/assets/application-*.js" > /dev/null \
    || { echo "precompile produced no application-*.js" >&2; false; }

STAGE="verifying compiled assets"
engine --verify-build

# --------------------------------------------------------------------------- restart
if [[ $RESTART == 0 ]]; then
    log "Built OK; not restarting (--no-restart). Run: systemctl restart zammad"
else
    STAGE="restarting Zammad"
    log "Restarting application server..."
    RESTARTED=1
    systemctl restart zammad
    STAGE="waiting for Zammad to answer"
    ok=0
    for _ in $(seq 1 60); do
        if curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:3000/api/v1/signshow; then ok=1; break; fi
        sleep 3
    done
    ((ok)) || { echo "Zammad did not come back within 3 minutes" >&2; false; }
fi

# --------------------------------------------------------------------------- done
trap - ERR INT TERM
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n -"$KEEP_BACKUPS" | xargs -r rm -rf
log "Finished. Originals/undo data: $BK   (undo: $0 --rollback $BK)"
log "Hard-refresh browsers (Ctrl+F5): icons.svg and the favicon are cached aggressively."
