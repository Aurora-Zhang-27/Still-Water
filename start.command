#!/bin/bash
# Double-click to open the Still Water pages; the browser opens once the app is up.
# Keep this Terminal window open; closing it (or pressing Ctrl+C) stops the pages.
# The daily audit and the weekly report are scheduled jobs and do not need this window.

cd "$(dirname "$0")" || exit 1
PORT=8501
URL="http://localhost:$PORT"

if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Already running; opening the browser: $URL"
  open "$URL"
  exit 0
fi

echo "Starting Still Water…"
echo "The browser opens $URL when it is ready."
echo "Close this window when you are done."
echo

# open the browser once the page actually responds (wait up to 60 seconds)
(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL"; then
      open "$URL"
      exit 0
    fi
    sleep 1
  done
) &

exec .venv/bin/streamlit run app/main.py --server.port "$PORT" --server.headless true
