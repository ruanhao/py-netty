#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Description:

rm -rf dist; python -m build . && twine upload -u ruanhao dist/*
