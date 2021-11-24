#!/bin/bash

# args=("$@")
cd /root || exit
# shellcheck source=/dev/null
source ./miniforge3/bin/activate base
./harness.py "$@"
# ./harness.py "${args[@]}"
