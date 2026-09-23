#!/usr/bin/env bash
# Delete the kind cluster (and all persistent volumes with it).
set -euo pipefail
kind delete cluster --name agents
