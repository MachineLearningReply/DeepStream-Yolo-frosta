#!/bin/bash

SESSION_NAME="uploader_session"
PYTHON_SCRIPT="/home/reply/frosta-retraining/upload_images_predictions/main.py"

# Check if the tmux session already exists
tmux has-session -t $SESSION_NAME 2>/dev/null

if [ $? != 0 ]; then
    echo "Starting new tmux session: $SESSION_NAME"
    tmux new-session -d -s $SESSION_NAME "python3 $PYTHON_SCRIPT"
else
    echo "Session $SESSION_NAME already exists. Attach using: tmux attach -t $SESSION_NAME"
fi

