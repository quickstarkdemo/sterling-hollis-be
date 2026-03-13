#!/bin/bash

set -euo pipefail

DEFAULT_ENV_FILE=".env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_step() {
  echo -e "${BLUE}$1${NC}"
}

print_success() {
  echo -e "${GREEN}$1${NC}"
}

print_warning() {
  echo -e "${YELLOW}$1${NC}"
}

print_error() {
  echo -e "${RED}$1${NC}"
}

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-n}"

  if [[ "$default" == "y" ]]; then
    prompt="$prompt [Y/n]: "
  else
    prompt="$prompt [y/N]: "
  fi

  while true; do
    read -r -p "$prompt" yn
    case "$yn" in
      [Yy]*) return 0 ;;
      [Nn]*) return 1 ;;
      "")
        [[ "$default" == "y" ]] && return 0 || return 1
        ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

check_prerequisites() {
  print_step "Checking prerequisites..."

  command -v git >/dev/null 2>&1 || { print_error "git is required"; exit 1; }
  command -v gh >/dev/null 2>&1 || { print_error "gh is required"; exit 1; }

  if ! gh auth status >/dev/null 2>&1; then
    print_error "GitHub CLI is not authenticated"
    echo "Run: gh auth login"
    exit 1
  fi
}

validate_env_file() {
  local env_file="$1"
  local required=(
    "PGHOST"
    "PGPORT"
    "PGDATABASE"
    "PGUSER"
    "PGPASSWORD"
    "DOCKERHUB_USER"
    "DOCKERHUB_TOKEN"
    "DOCKERHUB_IMAGE"
  )

  print_step "Validating $env_file..."
  for key in "${required[@]}"; do
    if ! grep -q "^${key}=" "$env_file"; then
      print_error "Missing required key: $key"
      exit 1
    fi

    local value
    value=$(grep "^${key}=" "$env_file" | tail -n 1 | cut -d '=' -f2-)
    value=$(echo "$value" | sed 's/^["'\'']\|["'\'']$//g')
    if [[ -z "$value" ]]; then
      print_error "Required key is empty: $key"
      exit 1
    fi
  done
}

set_version() {
  local current_version
  current_version=$(tr -d '[:space:]' < VERSION 2>/dev/null || true)
  current_version="${current_version:-0.1.0}"

  read -r -p "Version [$current_version]: " new_version
  new_version="${new_version:-$current_version}"

  if [[ "$new_version" != "$current_version" ]]; then
    echo "$new_version" > VERSION
    print_success "Updated VERSION to $new_version"
  else
    print_step "Keeping VERSION at $current_version"
  fi
}

upload_secrets() {
  local env_file="$1"
  print_step "Uploading GitHub secrets from $env_file..."
  "$SCRIPT_DIR/setup-secrets.sh" "$env_file"
}

commit_and_push() {
  local commit_message

  git status --short
  echo ""
  read -r -p "Commit message [Deploy product-db]: " commit_message
  commit_message="${commit_message:-Deploy product-db}"

  if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    git add .
  fi

  if git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
    print_warning "No tracked changes to commit."
    if prompt_yes_no "Trigger deploy workflow manually instead?" "y"; then
      gh workflow run deploy-self-hosted.yaml
      print_success "Workflow dispatch requested."
      return
    fi
    print_warning "Skipping push."
    return
  fi

  git commit -m "$commit_message"
  git push origin "$(git branch --show-current)"
}

main() {
  cd "$PROJECT_ROOT"

  local env_file="${1:-$DEFAULT_ENV_FILE}"

  if [[ ! -f "$env_file" ]]; then
    print_error "Environment file not found: $env_file"
    exit 1
  fi

  check_prerequisites
  validate_env_file "$env_file"
  set_version
  upload_secrets "$env_file"

  if prompt_yes_no "Commit and push changes to trigger deployment?" "y"; then
    commit_and_push
    print_success "Deployment push complete. Monitor GitHub Actions for progress."
  else
    print_warning "Secrets uploaded. Commit/push skipped."
  fi
}

main "$@"
