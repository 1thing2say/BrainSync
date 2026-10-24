#!/bin/bash
# Throwaway-container script: install the staged deb into a clean ubuntu:24.04 and smoke-test it.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends /out/python3-brainflow-cerelog_*.deb > /tmp/apt.log 2>&1 || { tail /tmp/apt.log; exit 1; }
echo "pulled in: $(grep -E '^Setting up' /tmp/apt.log | awk '{print $3}' | tr '\n' ' ')"
dpkg-query -W python3 python3-numpy python3-pkg-resources python3-setuptools 2>/dev/null || true
cd /
python3 -I -c 'from brainflow.board_shim import BoardShim, BoardIds; print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD)); print(BoardShim.get_eeg_channels(BoardIds.CERELOG_X8_BOARD)); print(BoardShim.get_version())'
python3 -I -c 'from brainflow.data_filter import DataFilter; print("datahandler", DataFilter.get_version())'
python3 -I -c 'from brainflow.ml_model import MLModel; print("ml", MLModel.get_version())'
echo "warnings on import:"; python3 -I -W always -c 'import brainflow' 2>&1 | head -3
