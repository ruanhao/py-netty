#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Description:

force=

while getopts ":f" opt; do
    case ${opt} in
    f)
        force=true
        ;;
    \?)
        echo "Invalid option: -$OPTARG" >&2
        exit 1
    esac
done

function get_epoch_from_setup_py() {
    cat setup.py | grep '^epoch' | sed -E "s/^epoch = (.*)/\1/"
}

function set_epoch_in_setup_py() {
    local epoch=$1
    sed -i -E "s/^epoch = (.*)/epoch = $epoch/" setup.py
}

current_epoch=`get_epoch_from_setup_py`
next_epoch=$((current_epoch + 1))


rm -rf dist

if git diff --exit-code >/dev/null; then
    echo "No changes in working directory"
    if git diff --exit-code --cached >/dev/null; then
        echo "No changes in index"
        if [[ -n $force ]]; then
            echo "Force build and upload"
            set_epoch_in_setup_py $next_epoch
        else
            echo "Working directory clean, skip"
            exit
        fi
    fi
else
    set_epoch_in_setup_py $next_epoch
fi

python -m build .

twine upload -u ruanhao dist/*
