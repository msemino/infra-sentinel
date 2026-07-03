#!/usr/bin/env sh
# Drive the Sentinel demo scenarios against the running mocks.
#
#   ./demo.sh new-problem   # inject one new High problem -> normal LLM cycle -> alert
#   ./demo.sh mass-outage   # inject a flood of new problems -> circuit breaker fires
#   ./demo.sh reset         # back to baseline
#   ./demo.sh inbox         # print the alert inbox
#
# The mocks share scenario state, so hitting any one control endpoint is enough.
set -eu

ZBX_PORT="${ZBX_PORT:-18080}"   # host port mapped to mock-zabbix if you exposed one
ACTION="${1:-new-problem}"

case "$ACTION" in
  inbox)
    echo "--- alert inbox (data/inbox.jsonl) ---"
    cat data/inbox.jsonl 2>/dev/null || echo "(empty — no alerts yet)"
    ;;
  new-problem|mass-outage|reset)
    # Flip the scenario inside the mock network via docker compose exec.
    docker compose exec -T mock-zabbix python -c "
import urllib.request
urllib.request.urlopen(urllib.request.Request('http://localhost:8080/control/$ACTION', method='POST'))
print('scenario set: $ACTION')
"
    echo "Watch the sentinel logs: docker compose logs -f sentinel"
    ;;
  *)
    echo "usage: $0 {new-problem|mass-outage|reset|inbox}" >&2
    exit 1
    ;;
esac
