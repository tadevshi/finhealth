#!/bin/bash
# Pull the configured Ollama model
# Usage: ./scripts/pull-ollama-model.sh [model_name]
# Default model: qwen2.5:1.5b

set -euo pipefail

MODEL="${1:-${LLM_MODEL:-qwen2.5:1.5b}}"

echo "Pulling Ollama model: $MODEL"
echo "This may take a few minutes depending on your connection..."

# Connect to the running Ollama container
docker compose -f docker-compose.self-hosted.yml exec ollama ollama pull "$MODEL"

echo "Model $MODEL is ready."
echo ""
echo "You can now start finhealth:"
echo "  docker compose -f docker-compose.self-hosted.yml up -d finhealth"
