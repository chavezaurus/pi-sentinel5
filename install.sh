#!/bin/bash

mkdir logs

## do a quick update
sudo apt-get update && sudo apt-get upgrade -y && sudo apt-get dist-upgrade -y;

## install python essentials
sudo apt-get install -y tmux chrony;
sudo systemctl enable chrony
sudo systemctl start chrony

curl -LsSf https://astral.sh/uv/install.sh | sh;
