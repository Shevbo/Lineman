#!/bin/bash
source /home/shectory/keymaster/.lineman-proxy.env
cd /home/shectory/workspaces/infra/lineman
exec .venv/bin/python main.py --port 9090 --host 0.0.0.0
