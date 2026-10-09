#!/usr/bin/env bash
# Production installer for Jira Sprint Monitor.
#
# On a fresh Linux VM:
#   sudo bash deploy.sh
#
# Installs Git, Python 3.9+, Nginx, and Gunicorn, clones the repository,
# writes config.yaml and .env, and starts the dashboard on port 80.
# Flask's built-in server is only for a laptop. In production Gunicorn
# serves the app on 127.0.0.1 and Nginx is the public front door.
# A weekday timer snapshots Jira at 08:00 and 14:00 in the timezone from
# config.yaml.
#
# The repository is public. A GitHub token is requested only when the
# clone or fetch is rejected.
#
# Re-running updates the code and restarts the service. Existing .env and
# config.yaml are kept.
#
#   JIRA_EMAIL        Optional. Written into .env when both this and the token are set.
#   JIRA_API_TOKEN    Optional. Leave unset and fill .env after install.
#   DOMAIN            Public hostname. Empty serves HTTP on this machine.
#   ENABLE_HTTPS      yes or no. Honoured only when DOMAIN is a hostname.
#   CERTBOT_EMAIL     Let's Encrypt contact. Defaults to JIRA_EMAIL.
#   GIT_TOKEN         Used only if the public clone is rejected.
#   INSTALL_DIR       Default: /opt/jira-sprint-monitor
#   APP_USER          Default: sprintmon
#   BRANCH            Default: main
#   REPO_URL          Default: https://github.com/asadsandyapp/Jira-monitoring.git
#   RUN_COLLECT       yes or no. Prompted on the first install.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/asadsandyapp/Jira-monitoring.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/jira-sprint-monitor}"
APP_USER="${APP_USER:-sprintmon}"
APP_PORT="${APP_PORT:-8000}"
DOMAIN="${DOMAIN:-}"
ENABLE_HTTPS="${ENABLE_HTTPS:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
RUN_COLLECT="${RUN_COLLECT:-}"
GIT_TOKEN="${GIT_TOKEN:-}"

SERVICE="jira-sprint-monitor"
COLLECT_SERVICE="${SERVICE}-collect"
NGINX_SITE=""
APP_GROUP=""
PY=""
PM=""
APP_TZ="Asia/Karachi"
USE_IPV6=1
ASKPASS_FILE=""

usage() {
  cat <<'EOF'
Usage: sudo bash deploy.sh

Install Jira Sprint Monitor on a Linux VM and start it in production.

The GitHub repository is cloned without credentials. If GitHub rejects
that clone, the script asks for a personal access token with read access.

The installer does not ask for the Jira token. After it finishes, edit
.env and set JIRA_EMAIL and JIRA_API_TOKEN, then restart the service.
Create the token at https://id.atlassian.com/manage-profile/security/api-tokens

Optional environment variables are listed in the header of this script.
Run again later to pull the latest main branch. .env and config.yaml stay.
EOF
}

die() {
  trap - ERR
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '\n==> %s\n' "$*"
}

warn() {
  printf '\nWARNING: %s\n' "$*" >&2
}

cleanup() {
  if [[ -n "$ASKPASS_FILE" ]]; then
    rm -f "$ASKPASS_FILE"
    ASKPASS_FILE=""
  fi
}

trap cleanup EXIT
trap 'printf "\nERROR: deploy failed near line %s\n" "$LINENO" >&2' ERR

need_tty() {
  [[ -r /dev/tty ]] || die "$1"
}

prompt_value() {
  local __name=$1 __text=$2 __secret=${3:-}
  local __value
  need_tty "Set ${__name} and run again (no terminal is available to ask for it)."
  if [[ "$__secret" == secret ]]; then
    read -r -s -p "$__text" __value < /dev/tty
    printf '\n' > /dev/tty
  else
    read -r -p "$__text" __value < /dev/tty
  fi
  printf -v "$__name" '%s' "$__value"
}

confirm_yes() {
  local answer
  need_tty "Set ${2:-the matching environment variable} so this question can be skipped."
  read -r -p "$1" answer < /dev/tty
  [[ ! "$answer" =~ ^[Nn] ]]
}

confirm_no() {
  local answer
  need_tty "Set ENABLE_HTTPS=yes or no."
  read -r -p "$1" answer < /dev/tty
  [[ "$answer" =~ ^[Yy] ]]
}

valid_secret() {
  local name=$1 value=$2
  [[ -n "$value" ]] || die "${name} is empty."
  if [[ "$value" == *[[:space:]]* || "$value" == *'#'* || "$value" == *'$'* || "$value" == *'`'* || "$value" == *'"'* || "$value" == *"'"* ]]; then
    die "${name} cannot contain whitespace, quotes, #, \$, or a backtick."
  fi
}

require_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return
  fi
  command -v sudo >/dev/null 2>&1 || die "Run this script as root."
  exec sudo \
    --preserve-env=JIRA_EMAIL,JIRA_API_TOKEN,DOMAIN,ENABLE_HTTPS,CERTBOT_EMAIL,GIT_TOKEN,INSTALL_DIR,APP_USER,BRANCH,REPO_URL,RUN_COLLECT \
    bash "$0" "$@"
}

detect_os() {
  if [[ "$(uname -s)" != Linux ]]; then
    die "This installer runs on a Linux VM. This machine is $(uname -s)."
  fi
  if command -v apt-get >/dev/null 2>&1; then
    PM=apt
  elif command -v dnf >/dev/null 2>&1; then
    PM=dnf
  elif command -v yum >/dev/null 2>&1; then
    PM=yum
  else
    die "Could not find apt-get, dnf, or yum."
  fi
  command -v systemctl >/dev/null 2>&1 || die "This installer needs systemd."
}

install_base_packages() {
  log "Installing Git, Python, Nginx, and certificates"
  case "$PM" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y \
        git curl ca-certificates tzdata \
        python3 python3-venv python3-pip \
        nginx
      ;;
    dnf|yum)
      "$PM" install -y \
        git curl ca-certificates tzdata \
        python3 python3-pip \
        nginx
      ;;
    *)
      die "Unsupported package manager: $PM"
      ;;
  esac
}

python_is_new_enough() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'
}

select_python() {
  local candidate
  if command -v python3 >/dev/null 2>&1 && python_is_new_enough python3; then
    PY=$(command -v python3)
    log "Using $("$PY" --version)"
    return
  fi
  log "System Python is older than 3.9. Looking for a newer package."
  if [[ "$PM" == apt ]]; then
    apt-get install -y python3.11 python3.11-venv || true
  else
    "$PM" install -y python3.11 || true
  fi
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_new_enough "$candidate"; then
      PY=$(command -v "$candidate")
      break
    fi
  done
  [[ -n "$PY" ]] || die "Python 3.9 or newer is required. Install it, then run this script again."
  log "Using $("$PY" --version)"
}

validate_settings() {
  [[ "$INSTALL_DIR" == /* ]] || die "INSTALL_DIR must be an absolute path."
  [[ "$INSTALL_DIR" != *" "* ]] || die "INSTALL_DIR cannot contain spaces."
  case "$INSTALL_DIR" in
    /|/opt|/usr|/usr/*|/home|/var|/etc|/srv|/root) die "Refusing to install into ${INSTALL_DIR}." ;;
  esac
  [[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "APP_USER must be a short lowercase system name."
  [[ "$APP_PORT" =~ ^[0-9]+$ ]] || die "APP_PORT must be a number."
  if (( APP_PORT < 1 || APP_PORT > 65535 )); then
    die "APP_PORT is out of range."
  fi
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "BRANCH contains unsupported characters."
  [[ "$REPO_URL" == https://github.com/* ]] || die "REPO_URL must be an https://github.com/ URL."
  if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
    die "DOMAIN must be a hostname, not a URL."
  fi
  case "$ENABLE_HTTPS" in
    ""|yes|no|y|n|true|false|1|0) ;;
    *) die "ENABLE_HTTPS must be yes or no." ;;
  esac
  case "$ENABLE_HTTPS" in
    y|true|1) ENABLE_HTTPS=yes ;;
    n|false|0) ENABLE_HTTPS=no ;;
  esac
  case "$RUN_COLLECT" in
    ""|yes|no|y|n) ;;
    *) die "RUN_COLLECT must be yes or no." ;;
  esac
  case "$RUN_COLLECT" in
    y) RUN_COLLECT=yes ;;
    n) RUN_COLLECT=no ;;
  esac
}

ensure_user() {
  local shell_path
  if ! id "$APP_USER" >/dev/null 2>&1; then
    log "Creating system user ${APP_USER}"
    shell_path=$(command -v nologin || true)
    [[ -n "$shell_path" ]] || shell_path=/usr/sbin/nologin
    useradd --system --home-dir "$INSTALL_DIR" --shell "$shell_path" "$APP_USER"
  fi
  APP_GROUP=$(id -gn "$APP_USER")
}

git_auth_failed() {
  grep -Eqi \
    'authentication failed|could not read Username|could not read Password|terminal prompts disabled|invalid username or (token|password)|support for password authentication|requested URL returned error: 401|requested URL returned error: 403|repository not found|repository .+ not found' \
    "$1"
}

with_git_auth() {
  local askpass rc=0
  if [[ -z "${GIT_TOKEN:-}" ]]; then
    GIT_TERMINAL_PROMPT=0 git -c "safe.directory=${INSTALL_DIR}" "$@" && rc=0 || rc=$?
    return "$rc"
  fi
  askpass=$(mktemp)
  ASKPASS_FILE=$askpass
  cat > "$askpass" <<'EOF'
#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\n' 'x-access-token' ;;
  *) printf '%s\n' "$GIT_TOKEN" ;;
esac
EOF
  chmod 700 "$askpass"
  GIT_TERMINAL_PROMPT=0 GIT_ASKPASS="$askpass" GIT_TOKEN="$GIT_TOKEN" \
    git -c "safe.directory=${INSTALL_DIR}" "$@" && rc=0 || rc=$?
  rm -f "$askpass"
  ASKPASS_FILE=""
  return "$rc"
}

prompt_git_token() {
  local token
  printf '\nGitHub rejected access to %s\n' "$REPO_URL" >&2
  printf 'The repository is public, so a token is only needed if it has been made private.\n' >&2
  need_tty "Set GIT_TOKEN to a GitHub personal access token with repository read access, then run again."
  read -r -s -p "GitHub personal access token: " token < /dev/tty
  printf '\n' > /dev/tty
  [[ -n "$token" ]] || die "No GitHub token entered."
  [[ "$token" != *[[:space:]]* ]] || die "The GitHub token cannot contain whitespace."
  GIT_TOKEN=$token
}

run_git() {
  local err rc
  err=$(mktemp)
  set +e
  with_git_auth "$@" >"$err" 2>&1
  rc=$?
  set -e
  cat "$err" >&2
  if [[ "$rc" -eq 0 ]]; then
    rm -f "$err"
    return 0
  fi
  if git_auth_failed "$err"; then
    rm -f "$err"
    return 10
  fi
  rm -f "$err"
  return 1
}

do_clone() {
  local rc=0
  rm -rf "$INSTALL_DIR"
  run_git clone --branch "$BRANCH" -- "$REPO_URL" "$INSTALL_DIR" || rc=$?
  return "$rc"
}

do_fetch() {
  local rc=0
  run_git -C "$INSTALL_DIR" fetch origin "$BRANCH" || rc=$?
  return "$rc"
}

with_token_retry() {
  local rc=0
  GIT_TOKEN=""
  "$@" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi
  if [[ "$rc" -ne 10 ]]; then
    return "$rc"
  fi
  if [[ -n "${PRESET_GIT_TOKEN:-}" ]]; then
    log "Public Git access was rejected. Retrying with GIT_TOKEN from the environment."
    GIT_TOKEN=$PRESET_GIT_TOKEN
    rc=0
    "$@" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
      return 0
    fi
    if [[ "$rc" -ne 10 ]]; then
      return "$rc"
    fi
    warn "That GIT_TOKEN was rejected."
  fi
  GIT_TOKEN=""
  prompt_git_token
  rc=0
  "$@" || rc=$?
  return "$rc"
}

sync_repository() {
  PRESET_GIT_TOKEN=${GIT_TOKEN:-}
  GIT_TOKEN=""
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Updating ${INSTALL_DIR}"
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    if ! with_token_retry do_fetch; then
      die "Could not fetch ${REPO_URL}"
    fi
    GIT_TOKEN=""
    with_git_auth -C "$INSTALL_DIR" checkout "$BRANCH"
    with_git_auth -C "$INSTALL_DIR" merge --ff-only "origin/${BRANCH}"
  else
    if [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
      die "${INSTALL_DIR} already exists and is not a git checkout. Move it aside and run again."
    fi
    log "Cloning ${REPO_URL}"
    if ! with_token_retry do_clone; then
      rm -rf "$INSTALL_DIR"
      die "Could not clone ${REPO_URL}"
    fi
  fi
  git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  unset GIT_TOKEN PRESET_GIT_TOKEN
  if [[ ! -f "${INSTALL_DIR}/requirements.txt" || ! -f "${INSTALL_DIR}/monitor/web.py" ]]; then
    die "Cloned ${REPO_URL} (${BRANCH}) but the application files are not there yet. Push the project to that branch, then run this script again."
  fi
}

as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    su -s /bin/bash "$APP_USER" -c "$(printf '%q ' "$@")"
  fi
}

install_python_deps() {
  local pip="${INSTALL_DIR}/.venv/bin/pip"
  log "Creating a virtualenv and installing Python packages"
  chown -R "${APP_USER}:${APP_GROUP}" "$INSTALL_DIR"
  if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
    as_app "$PY" -m venv "${INSTALL_DIR}/.venv"
  fi
  if ! as_app "$pip" install --no-cache-dir --upgrade pip; then
    log "pip needs build tools. Installing a compiler and retrying."
    case "$PM" in
      apt) apt-get install -y build-essential python3-dev ;;
      *) "$PM" install -y gcc python3-devel ;;
    esac
    as_app "$pip" install --no-cache-dir --upgrade pip
  fi
  as_app "$pip" install --no-cache-dir -r "${INSTALL_DIR}/requirements.txt" 'gunicorn>=22,<24'
}

ensure_config() {
  if [[ -f "${INSTALL_DIR}/config.yaml" ]]; then
    log "Keeping existing ${INSTALL_DIR}/config.yaml"
  else
    log "Writing ${INSTALL_DIR}/config.yaml from the example"
    cp "${INSTALL_DIR}/config.example.yaml" "${INSTALL_DIR}/config.yaml"
    chown "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}/config.yaml"
    chmod 640 "${INSTALL_DIR}/config.yaml"
  fi
  [[ -f "${INSTALL_DIR}/config.yaml" ]] || die "config.example.yaml is missing from the repository."
  APP_TZ=$(sed -n 's/^timezone:[[:space:]]*//p' "${INSTALL_DIR}/config.yaml" | head -1 || true)
  APP_TZ=${APP_TZ%%#*}
  read -r APP_TZ <<<"${APP_TZ}"
  [[ -n "$APP_TZ" ]] || APP_TZ="Asia/Karachi"
  [[ "$APP_TZ" =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]] || die "timezone in config.yaml is not a zone name."
}

read_existing_env() {
  local line key val
  JIRA_EMAIL=""
  JIRA_API_TOKEN=""
  [[ -f "${INSTALL_DIR}/.env" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key=${line%%=*}
    val=${line#*=}
    case "$key" in
      JIRA_EMAIL) JIRA_EMAIL=$val ;;
      JIRA_API_TOKEN) JIRA_API_TOKEN=$val ;;
    esac
  done < "${INSTALL_DIR}/.env"
}

write_env_file() {
  umask 077
  printf 'JIRA_EMAIL=%s\nJIRA_API_TOKEN=%s\n' "$JIRA_EMAIL" "$JIRA_API_TOKEN" > "${INSTALL_DIR}/.env"
  umask 022
  chown "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
}

ensure_env() {
  if [[ -f "${INSTALL_DIR}/.env" ]]; then
    read_existing_env
    chown "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
    if [[ -n "${JIRA_EMAIL:-}" && -n "${JIRA_API_TOKEN:-}" ]]; then
      log "Keeping existing ${INSTALL_DIR}/.env"
    else
      log "Leaving ${INSTALL_DIR}/.env for the Jira email and token"
    fi
    return
  fi
  if [[ -n "${JIRA_EMAIL:-}" && -n "${JIRA_API_TOKEN:-}" ]]; then
    valid_secret JIRA_EMAIL "$JIRA_EMAIL"
    valid_secret JIRA_API_TOKEN "$JIRA_API_TOKEN"
    [[ "$JIRA_EMAIL" == *@* ]] || die "JIRA_EMAIL does not look like an email address."
    log "Writing ${INSTALL_DIR}/.env from the environment"
    write_env_file
    return
  fi
  log "Creating ${INSTALL_DIR}/.env without a Jira token"
  JIRA_EMAIL=""
  JIRA_API_TOKEN=""
  write_env_file
  printf 'Add the Jira email and API token to %s/.env after this install.\n' "$INSTALL_DIR"
}

saved_domain() {
  local site name
  for site in \
    /etc/nginx/sites-available/jira-sprint-monitor \
    /etc/nginx/conf.d/jira-sprint-monitor.conf
  do
    [[ -f "$site" ]] || continue
    name=$(awk '$1 == "server_name" { gsub(/;/, "", $2); print $2; exit }' "$site")
    if [[ -n "$name" && "$name" != "_" ]]; then
      printf '%s\n' "$name"
      return
    fi
  done
}

resolve_domain() {
  local saved
  if [[ -z "$DOMAIN" ]]; then
    saved=$(saved_domain || true)
    if [[ -n "$saved" ]]; then
      DOMAIN=$saved
      return
    fi
  fi
  if [[ -n "$DOMAIN" || -f /etc/nginx/sites-available/jira-sprint-monitor || -f /etc/nginx/conf.d/jira-sprint-monitor.conf ]]; then
    return
  fi
  if [[ -r /dev/tty ]]; then
    prompt_value DOMAIN "Public hostname (leave blank to serve HTTP on this server's IP): "
    DOMAIN=${DOMAIN:-}
  fi
}

resolve_https() {
  if [[ -z "$DOMAIN" ]]; then
    ENABLE_HTTPS=no
    return
  fi
  if [[ "$DOMAIN" =~ ^[0-9.]+$ || "$DOMAIN" == *:* ]]; then
    warn "Skipping HTTPS because ${DOMAIN} is an IP address."
    ENABLE_HTTPS=no
    return
  fi
  if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    ENABLE_HTTPS=yes
    return
  fi
  if [[ -z "$ENABLE_HTTPS" ]]; then
    if [[ -r /dev/tty ]]; then
      if confirm_no "Issue a free HTTPS certificate for ${DOMAIN}? [y/N] "; then
        ENABLE_HTTPS=yes
      else
        ENABLE_HTTPS=no
      fi
    else
      ENABLE_HTTPS=no
    fi
  fi
}

listen_lines() {
  local port=$1 extra=${2:-}
  printf '    listen %s%s;\n' "$port" "$extra"
  if [[ "$USE_IPV6" -eq 1 ]]; then
    printf '    listen [::]:%s%s;\n' "$port" "$extra"
  fi
}

proxy_location() {
  cat <<EOF
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 180s;
    }
EOF
}

write_nginx() {
  local server_name cert
  if [[ -d /etc/nginx/sites-available ]]; then
    NGINX_SITE=/etc/nginx/sites-available/jira-sprint-monitor
  else
    NGINX_SITE=/etc/nginx/conf.d/jira-sprint-monitor.conf
  fi
  server_name=${DOMAIN:-_}
  cert="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  mkdir -p /var/www/html
  {
    echo "# Generated by deploy.sh. Re-run the installer to change it."
    echo "server {"
    listen_lines 80 " default_server"
    echo "    server_name ${server_name};"
    echo "    client_max_body_size 1m;"
    if [[ -n "$DOMAIN" && -f "$cert" ]]; then
      cat <<EOF
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
EOF
      echo "}"
      echo "server {"
      listen_lines 443 " ssl default_server"
      echo "    server_name ${server_name};"
      echo "    client_max_body_size 1m;"
      echo "    ssl_certificate ${cert};"
      echo "    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;"
      if [[ -f /etc/letsencrypt/options-ssl-nginx.conf ]]; then
        echo "    include /etc/letsencrypt/options-ssl-nginx.conf;"
      else
        echo "    ssl_protocols TLSv1.2 TLSv1.3;"
      fi
      if [[ -f /etc/letsencrypt/ssl-dhparams.pem ]]; then
        echo "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
      fi
      proxy_location
      echo "}"
    else
      proxy_location
      echo "}"
    fi
  } > "$NGINX_SITE"
  if [[ -d /etc/nginx/sites-enabled ]]; then
    ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/jira-sprint-monitor
    if [[ -e /etc/nginx/sites-enabled/default || -L /etc/nginx/sites-enabled/default ]]; then
      rm -f /etc/nginx/sites-enabled/default
      log "Disabled Nginx's stock default site so this dashboard answers on port 80."
    fi
  fi
}

install_nginx() {
  local err
  log "Configuring Nginx"
  write_nginx
  err=$(mktemp)
  if ! nginx -t >"$err" 2>&1; then
    if [[ "$USE_IPV6" -eq 1 ]] && grep -Eqi 'ipv6|address family|protocol not available' "$err"; then
      warn "Nginx rejected the IPv6 listener. Retrying with IPv4 only."
      USE_IPV6=0
      write_nginx
      rm -f "$err"
    else
      cat "$err" >&2
      rm -f "$err"
      die "Nginx configuration is invalid."
    fi
  else
    rm -f "$err"
  fi
  nginx -t
  systemctl enable nginx
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx
  else
    systemctl restart nginx
  fi
}

install_certbot_packages() {
  case "$PM" in
    apt) apt-get install -y certbot ;;
    *) "$PM" install -y certbot ;;
  esac
}

issue_certificate() {
  local email
  [[ "$ENABLE_HTTPS" == yes && -n "$DOMAIN" ]] || return 0
  if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    return 0
  fi
  log "Requesting a Let's Encrypt certificate for ${DOMAIN}"
  install_certbot_packages
  email=${CERTBOT_EMAIL:-$JIRA_EMAIL}
  [[ -n "$email" ]] || die "Set CERTBOT_EMAIL. Let's Encrypt needs a contact address."
  if ! certbot certonly --webroot -w /var/www/html -d "$DOMAIN" \
    --non-interactive --agree-tos -m "$email" --keep-until-expiring; then
    warn "HTTPS was not issued. The dashboard stays on HTTP. Point DNS at this server and run the script again with ENABLE_HTTPS=yes."
    ENABLE_HTTPS=no
    return 0
  fi
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
}

write_systemd() {
  log "Installing systemd services"
  cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Jira Sprint Monitor
After=network.target

[Service]
# One process: the sprint-load progress bar is stored in memory.
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/gunicorn \\
  --workers 1 \\
  --threads 4 \\
  --worker-class gthread \\
  --bind 127.0.0.1:${APP_PORT} \\
  --timeout 180 \\
  --graceful-timeout 30 \\
  --access-logfile - \\
  --error-logfile - \\
  monitor.web:create_app()
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

  cat > "/etc/systemd/system/${COLLECT_SERVICE}.service" <<EOF
[Unit]
Description=Jira Sprint Monitor snapshot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/python -m monitor.collect
NoNewPrivileges=true
PrivateTmp=true
EOF

  cat > "/etc/systemd/system/${COLLECT_SERVICE}.timer" <<EOF
[Unit]
Description=Snapshot the sprint on weekday mornings and afternoons

[Timer]
OnCalendar=Mon..Fri 08:00:00 ${APP_TZ}
OnCalendar=Mon..Fri 14:00:00 ${APP_TZ}
Persistent=true
RandomizedDelaySec=90

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE}.service"
  if systemd-analyze calendar "Mon..Fri 08:00:00 ${APP_TZ}" >/dev/null 2>&1; then
    rm -f /etc/cron.d/jira-sprint-monitor
    systemctl enable "${COLLECT_SERVICE}.timer"
    systemctl restart "${COLLECT_SERVICE}.timer"
  else
    warn "This systemd cannot schedule ${APP_TZ} directly. Using cron instead."
    systemctl disable --now "${COLLECT_SERVICE}.timer" >/dev/null 2>&1 || true
    cat > /etc/cron.d/jira-sprint-monitor <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CRON_TZ=${APP_TZ}
0 8,14 * * 1-5 ${APP_USER} cd ${INSTALL_DIR} && ${INSTALL_DIR}/.venv/bin/python -m monitor.collect
EOF
    chmod 644 /etc/cron.d/jira-sprint-monitor
  fi
}

open_firewall() {
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    log "Opening HTTP and HTTPS in firewalld"
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    log "Opening HTTP and HTTPS in ufw"
    ufw allow 80/tcp
    ufw allow 443/tcp
  fi
  if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == Enforcing ]]; then
    log "Allowing Nginx to connect to Gunicorn under SELinux"
    setsebool -P httpd_can_network_connect 1
  fi
}

start_app() {
  log "Starting the dashboard"
  systemctl restart "${SERVICE}.service"
  local i
  for ((i = 1; i <= 30; i++)); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${APP_PORT}/"; then
      return 0
    fi
    sleep 1
  done
  journalctl -u "${SERVICE}.service" -n 60 --no-pager || true
  die "The dashboard did not answer on 127.0.0.1:${APP_PORT}."
}

maybe_collect() {
  local answer
  read_existing_env
  if [[ -z "${JIRA_EMAIL:-}" || -z "${JIRA_API_TOKEN:-}" ]]; then
    log "Skipping the Jira snapshot until .env has JIRA_EMAIL and JIRA_API_TOKEN"
    return 0
  fi
  if [[ "$RUN_COLLECT" == yes ]]; then
    answer=yes
  elif [[ "$RUN_COLLECT" == no ]]; then
    return 0
  elif [[ -f "${INSTALL_DIR}/snapshots.db" ]]; then
    return 0
  elif [[ -r /dev/tty ]]; then
    if confirm_yes "Load the sprint from Jira now? [Y/n] "; then
      answer=yes
    else
      return 0
    fi
  else
    return 0
  fi
  if [[ "$answer" != yes ]]; then
    return 0
  fi
  log "Loading the sprint named in config.yaml"
  if ! systemd-run --wait --pipe --collect \
    --uid="$APP_USER" --gid="$APP_GROUP" \
    -p "WorkingDirectory=${INSTALL_DIR}" \
    -p "EnvironmentFile=${INSTALL_DIR}/.env" \
    -p "Environment=PYTHONUNBUFFERED=1" \
    "${INSTALL_DIR}/.venv/bin/python" -m monitor.collect; then
    warn "The first snapshot failed. Fix .env or config.yaml, then run: systemctl start ${COLLECT_SERVICE}.service"
  fi
}

public_url() {
  local ip4
  if [[ -n "$DOMAIN" && -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    printf 'https://%s/' "$DOMAIN"
    return
  fi
  if [[ -n "$DOMAIN" ]]; then
    printf 'http://%s/' "$DOMAIN"
    return
  fi
  ip4=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [[ -z "$ip4" ]]; then
    ip4=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
  fi
  if [[ -n "$ip4" ]]; then
    printf 'http://%s/' "$ip4"
  else
    printf 'http://<this-server>/'
  fi
}

summarize() {
  local url
  read_existing_env
  url=$(public_url)
  if ! curl -fsS -o /dev/null "http://127.0.0.1/"; then
    warn "Gunicorn is up, but Nginx did not answer on port 80."
  fi
  cat <<EOF

Dashboard: ${url}

Config:      ${INSTALL_DIR}/config.yaml
Credentials: ${INSTALL_DIR}/.env
Code:        ${INSTALL_DIR}
Service:     systemctl status ${SERVICE}
Logs:        journalctl -u ${SERVICE} -f
Snapshot:    systemctl start ${COLLECT_SERVICE}.service

Weekday snapshots run at 08:00 and 14:00 ${APP_TZ}.
Run this script again to pull updates. .env and config.yaml are left as they are.
EOF
  if [[ -z "${JIRA_EMAIL:-}" || -z "${JIRA_API_TOKEN:-}" ]]; then
    cat <<EOF

The Jira token is not set yet. Edit ${INSTALL_DIR}/.env:

  JIRA_EMAIL=you@company.com
  JIRA_API_TOKEN=...

Then restart: systemctl restart ${SERVICE}
EOF
  fi
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  if [[ $# -gt 0 ]]; then
    die "Unknown argument: $1. Use --help."
  fi
  require_root "$@"
  validate_settings
  detect_os
  install_base_packages
  select_python
  ensure_user
  sync_repository
  install_python_deps
  ensure_config
  ensure_env
  resolve_domain
  resolve_https
  validate_settings
  write_systemd
  open_firewall
  install_nginx
  issue_certificate
  if [[ "$ENABLE_HTTPS" == yes ]]; then
    write_nginx
    nginx -t
    systemctl reload nginx
  fi
  start_app
  maybe_collect
  summarize
}

main "$@"
