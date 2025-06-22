#!/bin/bash

base=$(dirname "$0")

source $base/venv/bin/activate

$base/venv/bin/python3.10 $base/rvc.py "$@"
