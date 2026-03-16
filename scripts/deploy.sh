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

suggest_next_version() {
  local version="$1"

  if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
    echo "$version"
    return
  fi

  awk -F. '{
    $NF = $NF + 1;
    out = $1;
    for (i = 2; i <= NF; i += 1) {
      out = out "." $i;
    }
    print out;
  }' <<< "$version"
}

set_version() {
  local current_version
  local suggested_version
  local prompt
  current_version=$(tr -d '[:space:]' < VERSION 2>/dev/null || true)
  current_version="${current_version:-0.1.0}"
  suggested_version="$(suggest_next_version "$current_version")"

  if [[ "$suggested_version" == "$current_version" ]]; then
    prompt="Version [$current_version]: "
  else
    prompt="Version [$suggested_version] (current: $current_version, '=' to keep current): "
  fi

  read -r -p "$prompt" new_version
  new_version="${new_version:-$suggested_version}"
  if [[ "$new_version" == "=" ]]; then
    new_version="$current_version"
  fi

  if [[ ! "$new_version" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
    print_error "Invalid version format: $new_version (expected dot-separated numeric segments)"
    exit 1
  fi

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

generate_commit_message() {
  local staged_files
  staged_files="$(git diff --cached --name-only)"

  if [[ -z "$staged_files" ]]; then
    echo "Deploy product-db"
    return
  fi

  local file_count=0
  local non_version_count=0
  local has_app=0
  local has_tests=0
  local has_docs=0
  local has_scripts=0
  local has_deploy=0
  local has_db=0
  local has_version=0
  local target_first=""
  local target_second=""
  local path

  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    ((file_count += 1))
    case "$path" in
      app/*) has_app=1 ;;
      tests/*) has_tests=1 ;;
      docs/*|README.md) has_docs=1 ;;
      scripts/*) has_scripts=1 ;;
      .github/*|deploy/*|Dockerfile|docker-compose.yml) has_deploy=1 ;;
      alembic/*) has_db=1 ;;
      VERSION) has_version=1 ;;
    esac

    if [[ "$path" != "VERSION" ]]; then
      ((non_version_count += 1))
      local base_name
      base_name="$(basename "$path")"

      if [[ -z "$target_first" ]]; then
        target_first="$base_name"
      elif [[ "$base_name" != "$target_first" && -z "$target_second" ]]; then
        target_second="$base_name"
      fi
    fi
  done <<< "$staged_files"

  local scope_text=""
  [[ "$has_app" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }workspace"
  [[ "$has_db" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }db"
  [[ "$has_scripts" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }scripts"
  [[ "$has_deploy" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }deploy"
  [[ "$has_tests" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }tests"
  [[ "$has_docs" -eq 1 ]] && scope_text="${scope_text:+$scope_text, }docs"
  if [[ -z "$scope_text" ]]; then
    if [[ "$has_version" -eq 1 ]]; then
      scope_text="version"
    else
      scope_text="updates"
    fi
  fi

  local current_version
  current_version="$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"

  if [[ "$has_version" -eq 1 && "$file_count" -eq 1 && -n "$current_version" ]]; then
    echo "chore: bump VERSION to ${current_version}"
    return
  fi

  local target_text="staged changes"
  local captured_targets=0
  [[ -n "$target_first" ]] && ((captured_targets += 1))
  [[ -n "$target_second" ]] && ((captured_targets += 1))
  local target_suffix_count=$((non_version_count - captured_targets))
  if [[ "$captured_targets" -eq 1 ]]; then
    target_text="$target_first"
  elif [[ "$captured_targets" -ge 2 ]]; then
    target_text="${target_first} and ${target_second}"
  fi
  if [[ "$target_suffix_count" -gt 0 ]]; then
    target_text="${target_text} +${target_suffix_count} files"
  fi

  if [[ -n "$current_version" ]]; then
    echo "deploy(${scope_text}): ${target_text} (v${current_version})"
  else
    echo "deploy(${scope_text}): ${target_text}"
  fi
}

write_deploy_notes() {
  local commit_subject="$1"
  local notes_file="$DEPLOY_STATE_DIR/deploy-notes-latest.md"
  local generated_at_utc
  local current_version
  local current_branch
  local base_sha
  local shortstat

  mkdir -p "$DEPLOY_STATE_DIR"
  generated_at_utc="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  current_version="$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"
  current_branch="$(git branch --show-current)"
  base_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  shortstat="$(git diff --cached --shortstat | sed 's/^ *//')"
  shortstat="${shortstat:-0 files changed}"

  {
    echo "# Deploy Notes"
    echo ""
    echo "- Generated (UTC): ${generated_at_utc}"
    echo "- Branch: ${current_branch}"
    echo "- Base SHA: ${base_sha}"
    if [[ -n "$current_version" ]]; then
      echo "- Version: ${current_version}"
    fi
    echo "- Proposed commit: ${commit_subject}"
    echo "- Diff summary: ${shortstat}"
    echo ""
    echo "## Staged files"
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      echo "- \`${line}\`"
    done <<< "$(git diff --cached --name-status)"
  } > "$notes_file"

  echo "$notes_file"
}

append_deploy_history() {
  local commit_subject="$1"
  local history_file="$DEPLOY_STATE_DIR/deploy-history.md"
  local generated_at_utc
  local current_version
  local current_branch
  local head_sha
  local shortstat

  mkdir -p "$DEPLOY_STATE_DIR"
  generated_at_utc="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  current_version="$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"
  current_branch="$(git branch --show-current)"
  head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  shortstat="$(git show --shortstat --format='' HEAD | sed 's/^ *//')"
  shortstat="${shortstat:-0 files changed}"

  {
    echo "## ${generated_at_utc} | ${commit_subject}"
    echo ""
    echo "- Branch: ${current_branch}"
    echo "- Commit: ${head_sha}"
    if [[ -n "$current_version" ]]; then
      echo "- Version: ${current_version}"
    fi
    echo "- Summary: ${shortstat}"
    echo "- Files:"
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      echo "  - ${line}"
    done <<< "$(git show --name-status --format='' HEAD)"
    echo ""
  } >> "$history_file"

  echo "$history_file"
}

commit_and_push() {
  local commit_message
  local auto_commit_message
  local deploy_notes_file
  local deploy_history_file
  local unstaged_tracked
  local untracked_files

  git status --short

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

  auto_commit_message="$(generate_commit_message)"
  deploy_notes_file="$(write_deploy_notes "$auto_commit_message")"
  print_step "Generated deploy notes: ${deploy_notes_file#$PROJECT_ROOT/}"
  echo ""
  read -r -p "Commit message [$auto_commit_message]: " commit_message
  commit_message="${commit_message:-$auto_commit_message}"

  git commit -m "$commit_message"
  git push origin "$(git branch --show-current)"
  deploy_history_file="$(append_deploy_history "$commit_message")"
  print_step "Updated deploy history: ${deploy_history_file#$PROJECT_ROOT/}"
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
