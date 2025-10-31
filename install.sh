#!/bin/sh
python -m venv venv
source ./venv/bin/activate.csh

echo Enter Venv for install python packages
pip install -r requirements.txt
