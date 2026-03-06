#!/bin/bash
# Kill the codegen agent dashboard server
pids=$(lsof -ti :7070 2>/dev/null)
if [ -n "$pids" ]; then
    echo "Killing PIDs: $pids"
    echo "$pids" | xargs kill -9
    echo "Done."
else
    echo "Nothing running on port 7070."
fi
