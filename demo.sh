#!/usr/bin/env sh
# Drive the Sentinel demo scenarios against the running mocks.
#
#   ./demo.sh new-problem   # one new High problem -> ALERT, then REPORT
#   ./demo.sh transient     # an incident that opened AND closed between two rounds.
#                           # It never appears in the active-problem query; only the event
#                           # window sees it. Under v1 this produced no alert at all.
#   ./demo.sh slow-model    # the model stalls for 30s. The ALERT still goes out immediately;
#                           # only the REPORT waits. This is the reason v2 exists.
#   ./demo.sh break-model   # the model returns 500. ALERT arrives, then a short note that
#                           # the investigation failed. Never silence.
#   ./demo.sh mass-outage   # a flood of new problems -> circuit breaker, one terse message
#   ./demo.sh reset         # back to baseline
#   ./demo.sh inbox         # print the alert inbox
#
# Each mock role is its own process with its own scenario state, so model behaviour is set
# on the model and monitoring behaviour on the monitoring.
set -eu

ACTION="${1:-new-problem}"

_control() {
  docker compose exec -T "$1" python -c "
import urllib.request
urllib.request.urlopen(urllib.request.Request('http://localhost:8080/control/$2', method='POST'))
print('scenario set on $1: $2')
"
}

case "$ACTION" in
  inbox)
    echo "--- alert inbox (data/inbox.jsonl) ---"
    cat data/inbox.jsonl 2>/dev/null || echo "(empty — no alerts yet)"
    ;;
  new-problem|mass-outage|transient)
    _control mock-zabbix "$ACTION"
    echo "Watch the sentinel logs: docker compose logs -f sentinel"
    ;;
  slow-model|break-model)
    _control mock-ollama "$ACTION"
    echo "Watch the sentinel logs: docker compose logs -f sentinel"
    echo "The ALERT should appear before the model is ever contacted."
    ;;
  reset)
    _control mock-zabbix reset
    _control mock-ollama reset
    ;;
  *)
    echo "usage: $0 {new-problem|transient|slow-model|break-model|mass-outage|reset|inbox}" >&2
    exit 1
    ;;
esac
