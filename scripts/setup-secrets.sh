#!/bin/bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if ! command -v gh >/dev/null 2>&1; then
  echo -e "${RED}GitHub CLI (gh) is required${NC}"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo -e "${RED}GitHub CLI is not authenticated${NC}"
  echo "Run: gh auth login"
  exit 1
fi

if [ $# -lt 1 ]; then
  echo -e "${RED}Usage: $0 <env-file>${NC}"
  exit 1
fi

ENV_FILE="$1"

if [ ! -f "$ENV_FILE" ]; then
  echo -e "${RED}Environment file not found: $ENV_FILE${NC}"
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
SKIP_VARIABLES=("ENV_FILE_HASH")
SECRET_COUNT=0
SKIPPED_COUNT=0

validate_dockerhub_image() {
  local image="$1"
  local lowercase_image

  lowercase_image="$(printf '%s' "$image" | tr '[:upper:]' '[:lower:]')"

  if [[ "$image" != "$lowercase_image" ]]; then
    echo -e "${RED}DOCKERHUB_IMAGE must be lowercase for Docker image repositories: $image${NC}"
    exit 1
  fi

  if [[ "$image" != */* ]]; then
    echo -e "${RED}DOCKERHUB_IMAGE must include the Docker Hub namespace, for example quickstark/sterling-hollis-be${NC}"
    exit 1
  fi

  if [[ "$image" == *:* ]]; then
    echo -e "${RED}DOCKERHUB_IMAGE should not include a tag; the deploy workflow adds :latest and version tags.${NC}"
    exit 1
  fi

  if [[ ! "$image" =~ ^[a-z0-9]+([._-][a-z0-9]+)*/[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
    echo -e "${RED}DOCKERHUB_IMAGE has an invalid Docker Hub repository format: $image${NC}"
    exit 1
  fi
}

while IFS= read -r line || [ -n "$line" ]; do
  if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
    continue
  fi

  if [[ "$line" =~ ^([^=]+)=(.*)$ ]]; then
    KEY="${BASH_REMATCH[1]}"
    VALUE="${BASH_REMATCH[2]}"
    VALUE=$(echo "$VALUE" | sed 's/^["'\'']\|["'\'']$//g')

    if [[ " ${SKIP_VARIABLES[*]} " =~ " ${KEY} " ]]; then
      ((SKIPPED_COUNT+=1))
      continue
    fi

    if [[ -z "$VALUE" || "$VALUE" =~ ^(your-|sk-your-|secret_your-) ]]; then
      echo -e "${YELLOW}Skipping $KEY (empty or placeholder)${NC}"
      ((SKIPPED_COUNT+=1))
      continue
    fi

    if [[ "$KEY" == "DOCKERHUB_IMAGE" ]]; then
      validate_dockerhub_image "$VALUE"
    fi

    echo -e "${GREEN}Setting secret: $KEY${NC}"
    echo "$VALUE" | gh secret set "$KEY" --repo "$REPO"
    ((SECRET_COUNT+=1))
  fi
done < "$ENV_FILE"

echo ""
echo -e "${GREEN}Uploaded $SECRET_COUNT secrets${NC}"
echo -e "${YELLOW}Skipped $SKIPPED_COUNT entries${NC}"
