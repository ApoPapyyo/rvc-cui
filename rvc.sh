#!/bin/bash

base=$(realpath "`dirname $0`")

if ! [ -e "$base/venv" ]; then
    "$base/setup.sh"
fi

source "$base/venv/bin/activate"

"$base/venv/bin/python3.10" "$base/rvc.py" "$@"
