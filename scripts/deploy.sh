#!/bin/bash

set -euo pipefail

DEFAULT_ENV_FILE=".env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_STATE_DIR="$PROJECT_ROOT/.deploy"

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

hash_file() {
  local file_path="$1"

  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file_path" | awk '{print $1}'
    return
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file_path" | awk '{print $1}'
    return
  fi

  print_error "Either shasum or sha256sum is required"
  exit 1
}

hash_text() {
  local text="$1"

  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$text" | shasum -a 256 | awk '{print $1}'
    return
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$text" | sha256sum | awk '{print $1}'
    return
  fi

  print_error "Either shasum or sha256sum is required"
  exit 1
}

env_state_file() {
  local env_file="$1"
  local env_abs_path
  local env_id

  env_abs_path="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"
  env_id="$(hash_text "$env_abs_path")"
  echo "$DEPLOY_STATE_DIR/env-sync-${env_id}.sha256"
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
  local state_file
  local current_hash
  local previous_hash=""

  state_file="$(env_state_file "$env_file")"
  current_hash="$(hash_file "$env_file")"

  if [[ -f "$state_file" ]]; then
    previous_hash="$(cut -d ' ' -f1 < "$state_file")"
  fi

  if [[ -n "$previous_hash" && "$previous_hash" == "$current_hash" ]]; then
    print_step "Environment file unchanged; skipping GitHub secret upload."
    return
  fi

  print_step "Uploading GitHub secrets from $env_file..."
  "$SCRIPT_DIR/setup-secrets.sh" "$env_file"
  mkdir -p "$DEPLOY_STATE_DIR"
  printf '%s  %s\n' "$current_hash" "$env_file" > "$state_file"
  print_success "Recorded env sync fingerprint for $env_file"
}

commit_and_push() {
  local commit_message
  local unstaged_tracked
  local untracked_files

  git status --short
  echo ""
  read -r -p "Commit message [Deploy product-db]: " commit_message
  commit_message="${commit_message:-Deploy product-db}"

  unstaged_tracked="$(git diff --name-only)"
  untracked_files="$(git ls-files --others --exclude-standard)"

  if [[ -n "$untracked_files" ]]; then
    print_error "Refusing to deploy with untracked files present."
    echo "$untracked_files"
    echo ""
    echo "Stage or ignore these files intentionally before deploying."
    exit 1
  fi

  if [[ -n "$unstaged_tracked" ]]; then
    if [[ "$unstaged_tracked" == "VERSION" ]]; then
      git add VERSION
    else
      print_error "Refusing to deploy with unstaged tracked changes."
      echo "$unstaged_tracked"
      echo ""
      echo "Stage exactly what you want deployed, then rerun the script."
      exit 1
    fi
  fi

  if git diff --cached --quiet; then
    print_warning "No staged changes to commit."
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
