#!/bin/bash

set -e
set -x

cd "$(dirname "$0")/.."
pwd

# Create config dir if not present
if [[ ! -d "${PWD}/config" ]]; then
    mkdir -p "${PWD}/config"
    hass --config "${PWD}/config" --script ensure_config
fi

# Overwrite configuration.yaml if provided
if [ -f ${PWD}/.devcontainer/configuration.yaml ]; then
    rm -f ${PWD}/config/configuration.yaml
    ln -s ${PWD}/.devcontainer/configuration.yaml ${PWD}/config/configuration.yaml
fi

# custom_components lives at the repo root so it's importable as a normal
# package too (tests, tooling); symlink it into config/ so Home Assistant's
# component loader (which only scans <config_dir>/custom_components) finds it.
if [ ! -d ${PWD}/config/custom_components ]; then
    mkdir -p ${PWD}/config/custom_components
fi

if [ ! -e ${PWD}/config/custom_components/reolink_manager ]; then
    rm -f ${PWD}/config/custom_components/reolink_manager
    ln -s ${PWD}/custom_components/reolink_manager \
          ${PWD}/config/custom_components/reolink_manager
fi

export PYTHONPATH="${PWD}:${PWD}/config:${PYTHONPATH}"

# Start Home Assistant
hass --config "${PWD}/config" --debug
