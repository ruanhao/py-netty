#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Description:

# rm -rf dist; python -m build . && twine upload -u ruanhao dist/*

tempdir="$(mktemp -d)"
file "$tempdir"
python setup.py sdist -d "$tempdir" bdist_wheel -d "$tempdir" && twine upload -u ruanhao $tempdir/*
