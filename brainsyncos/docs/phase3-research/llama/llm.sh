#!/bin/bash
set -u
export DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
apt-get update -qq >/dev/null && apt-get install -y -qq python3-venv curl libgomp1 >/dev/null 2>&1
cp -r /src /work; mkdir -p /opt/llama && tar -xzf /dl/llama/llama.tgz -C /opt/llama --strip-components=1
ls /opt/llama | tr '\n' ' '; echo
ldd /opt/llama/llama-server | grep -E "not found|ssl|gomp|curl" 
/opt/llama/llama-server --version 2>&1 | tail -2
(/opt/llama/llama-server -m /dl/model/qwen.gguf --jinja --host 127.0.0.1 --port 8080 -c 8192 -t 4 > /tmp/ls.log 2>&1 &)
python3 -m venv /v && /v/bin/pip install -q "anthropic>=2.3,<3" openai pydantic pyyaml 2>&1 | tail -1
for i in $(seq 60); do curl -sf http://127.0.0.1:8080/health >/dev/null && break; sleep 1; done
curl -s http://127.0.0.1:8080/health; echo
curl -s -o /dev/null -w "POST /v1/messages (empty) -> %{http_code}\n" -X POST http://127.0.0.1:8080/v1/messages -H 'content-type: application/json' -d '{}'
/v/bin/python /scripts/llm_probe.py 2>&1
echo "--- server log tail"; grep -iE "error|warn|unsupported|output_config|strict" /tmp/ls.log | head -10
