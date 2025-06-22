#!/bin/bash

base=$(dirname "$0")

source $base/venv/bin/activate

python3 $base/rvc.py "$@"
