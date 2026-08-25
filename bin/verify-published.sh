#!/usr/bin/env bash
# verify-published.sh
# Consumes a PUBLISHED release from its public registry and asserts it is
# usable by a consumer. This is deliberately not a workspace check: every
# in-repo check resolves the artifact by directory, which is exactly why
# `go/v2.0.0` passed `go build`, `go vet`, `go test -race`, staticcheck, the
# conformance suite and all 18 PR contexts while being unfetchable (#50481).
#
# Each lane installs into a throwaway directory OUTSIDE this repo and asserts:
#   python — pip install from PyPI, import, installed version == requested
#   node   — npm install from registry.npmjs.org, import, version == requested,
#            and the shipped bundle actually contains a baseline metric name
#   go     — go get via proxy.golang.org into a clean module, build AND run
#
# Publication is irreversible, so this can only report, never prevent. Its
# value is turning "found weeks later by a consumer" into "found in a minute",
# which is the margin that made go/v2.0.0 a dangling tag rather than a
# checksum mismatch in somebody's build.
#
# Usage:
#   verify-published.sh <lane> <version>
#     lane     python | node | go
#     version  python/node: bare semver (2.0.0)
#              go:          tag-style, with the v (v2.0.1)
#
# Environment:
#   RETRIES  attempts while waiting for registry propagation (default 10)
#   DELAY    seconds between attempts (default 15)
#
# Exit codes:
#   0 — the published artifact resolved, imported, and reported the right version
#   1 — verification failed (unresolvable, wrong version, import/build/run failed)
#   2 — bad invocation / missing toolchain

set -euo pipefail

RETRIES="${RETRIES:-10}"
DELAY="${DELAY:-15}"

GO_MODULE_BASE="github.com/Simmons-Systems/simsys-metrics/go"
PY_PACKAGE="simsys-metrics"
NODE_PACKAGE="@simsys/metrics"

die_usage() { echo "usage: $0 <python|node|go> <version>" >&2; exit 2; }

[ "$#" -eq 2 ] || die_usage
LANE="$1"
VERSION="$2"
[ -n "$VERSION" ] || die_usage

WORKDIR=""
cleanup() {
  # Guarded: never let an unset/empty variable turn this into a bare `rm -rf`.
  if [ -n "${WORKDIR:-}" ] && [ -d "${WORKDIR:-}" ]; then
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "MISSING TOOL: $1" >&2; exit 2; }
}

# Retry wrapper, for registry propagation only. Bounded on purpose: an
# unattended wait that cannot end is a bug, not patience.
with_retries() {
  local attempt=1
  until "$@"; do
    if [ "$attempt" -ge "$RETRIES" ]; then
      echo "  still failing after ${RETRIES} attempts (~$(( RETRIES * DELAY ))s)" >&2
      return 1
    fi
    echo "  attempt ${attempt}/${RETRIES} failed; retrying in ${DELAY}s (registry propagation)"
    attempt=$(( attempt + 1 ))
    sleep "$DELAY"
  done
  return 0
}

# The repo root is derived from this script's own location, not from $PWD —
# $PWD is wherever the caller happens to stand (often /tmp, which would make
# a $PWD-based guard reject every valid workdir).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORKDIR="$(mktemp -d)"
case "$WORKDIR" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "refusing to verify from inside the repo: $WORKDIR" >&2
    exit 2
    ;;
esac

echo "=== verify-published: lane=${LANE} version=${VERSION}"
echo "    workdir: ${WORKDIR} (outside the repo)"

case "$LANE" in
  python)
    need python3
    python3 -m venv "$WORKDIR/venv"
    echo "--- pip install ${PY_PACKAGE}==${VERSION} from PyPI"
    with_retries "$WORKDIR/venv/bin/pip" install --quiet --no-cache-dir \
      --index-url https://pypi.org/simple \
      "${PY_PACKAGE}==${VERSION}" \
      || { echo "FAIL: ${PY_PACKAGE}==${VERSION} did not install from PyPI" >&2; exit 1; }

    echo "--- import the published package and assert its version"
    "$WORKDIR/venv/bin/python" - "$VERSION" <<'PYEOF'
import sys
import importlib.metadata as md

want = sys.argv[1]
from simsys_metrics import install  # noqa: F401  — the import must succeed

got = md.version("simsys-metrics")
print(f"    imported simsys_metrics.install: {install!r}")
print(f"    installed version: {got}")
if got != want:
    print(f"FAIL: installed {got}, expected {want}", file=sys.stderr)
    sys.exit(1)
PYEOF
    ;;

  node)
    need node
    need npm
    cd "$WORKDIR"
    npm init -y >/dev/null 2>&1
    echo "--- npm install ${NODE_PACKAGE}@${VERSION} from registry.npmjs.org"
    with_retries npm install --silent --no-audit --no-fund \
      --registry https://registry.npmjs.org \
      "${NODE_PACKAGE}@${VERSION}" \
      || { echo "FAIL: ${NODE_PACKAGE}@${VERSION} did not install from npm" >&2; exit 1; }

    echo "--- assert the installed version"
    got="$(node -p "require('./node_modules/@simsys/metrics/package.json').version")"
    echo "    installed version: ${got}"
    if [ "$got" != "$VERSION" ]; then
      echo "FAIL: installed ${got}, expected ${VERSION}" >&2
      exit 1
    fi

    echo "--- import the published package"
    node --input-type=module -e "
      import * as m from '@simsys/metrics';
      if (typeof m.install !== 'function') {
        console.error('FAIL: install export missing from the published package');
        process.exit(1);
      }
      console.log('    exports:', Object.keys(m).sort().join(','));
    "

    echo "--- assert a baseline metric name is present in the shipped bundle"
    if ! grep -rq "simsys_build_info" node_modules/@simsys/metrics/dist; then
      echo "FAIL: simsys_build_info absent from the shipped bundle" >&2
      exit 1
    fi
    echo "    simsys_build_info present in dist/"
    ;;

  go)
    need go
    # Major >= 2 requires the /vN suffix in the module path. Derived here
    # rather than hardcoded, because a mismatch between the tag's major and
    # the module path IS the #50481 defect.
    major="$(printf '%s' "$VERSION" | sed -E 's|^v([0-9]+)\..*|\1|')"
    case "$major" in
      ''|*[!0-9]*)
        echo "FAIL: cannot parse a major version from '${VERSION}' (want vN.N.N)" >&2
        exit 2
        ;;
    esac
    if [ "$major" -ge 2 ]; then
      module="${GO_MODULE_BASE}/v${major}"
    else
      module="${GO_MODULE_BASE}"
    fi
    echo "    derived module path: ${module}"

    cd "$WORKDIR"
    # GOWORK=off so no workspace can resolve this by directory instead of via
    # the proxy — directory resolution is what hid the original defect.
    export GOWORK=off
    export GOPROXY="https://proxy.golang.org,direct"
    export GOFLAGS=-mod=mod

    go mod init verify >/dev/null 2>&1 || { echo "FAIL: go mod init" >&2; exit 1; }
    cat > main.go <<GOEOF
package main

import (
	"fmt"

	simsysmetrics "${module}"
)

func main() {
	// Any exported symbol will do; SafeLabel is pure and needs no server.
	got := simsysmetrics.SafeLabel("alpha", []string{"alpha", "beta"})
	if got != "alpha" {
		panic("SafeLabel returned " + got)
	}
	fmt.Println("    consumed ${module} ${VERSION}; SafeLabel ->", got)
}
GOEOF

    echo "--- go get ${module}@${VERSION} via proxy.golang.org"
    with_retries go get "${module}@${VERSION}" \
      || { echo "FAIL: ${module}@${VERSION} is not fetchable from the proxy" >&2; exit 1; }

    echo "--- build and run against the published module"
    go build ./... || { echo "FAIL: build against the published module" >&2; exit 1; }
    go run . || { echo "FAIL: run against the published module" >&2; exit 1; }

    echo "--- assert the resolved version is the requested one"
    resolved="$(go list -m "${module}" | awk '{print $2}')"
    echo "    resolved: ${resolved}"
    if [ "$resolved" != "$VERSION" ]; then
      echo "FAIL: resolved ${resolved}, expected ${VERSION}" >&2
      exit 1
    fi
    ;;

  *)
    die_usage
    ;;
esac

echo "=== PASS: ${LANE} ${VERSION} is published and consumable"
