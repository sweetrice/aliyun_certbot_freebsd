#!/bin/sh
python -m venv venv
source ./venv/bin/activate.csh #linux 用户执行 source ./venv/bin/activate

echo Enter Venv for install python packages
pip install -r requirements.txt
