#!/bin/bash

## sync the time
sudo ntpdate -q 0.us.pool.ntp.org;

tmux start-server
tmux new-session -d -s sentinel
tmux send-keys -t sentinel:0 "uv run sentinel.py" ENTER

echo "To view the Pi-Sentinel interface, attach any browser to [raspberry pi ip_address]:9090"
echo "To see Pi-Sentinel messages, type in: tmux attach"
echo "To stop Pi-Sentinel, type in: tmux kill-session"
