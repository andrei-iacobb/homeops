#!/usr/bin/env bash
set -Eeuo pipefail

# Log messages with different levels
function log() {
    local level="${1:-info}"
    shift

    # Get priority for a level (1=debug, 2=info, 3=warn, 4=error)
    get_priority() {
        case "${1:-info}" in
            debug) echo 1 ;;
            info)  echo 2 ;;
            warn)  echo 3 ;;
            error) echo 4 ;;
            *)     echo 2 ;;
        esac
    }

    local current_priority
    current_priority=$(get_priority "$level")
    local configured_level="${LOG_LEVEL:-info}"
    local configured_priority
    configured_priority=$(get_priority "$configured_level")

    # Skip log messages below the configured log level
    if [[ "$current_priority" -lt "$configured_priority" ]]; then
        return
    fi

    # Get color for level
    get_color() {
        case "${1:-info}" in
            debug) echo "\033[1m\033[38;5;63m" ;;
            info)  echo "\033[1m\033[38;5;87m" ;;
            warn)  echo "\033[1m\033[38;5;192m" ;;
            error) echo "\033[1m\033[38;5;198m" ;;
            *)     echo "\033[1m\033[38;5;87m" ;;
        esac
    }
    local color
    color=$(get_color "$level")
    local msg="$1"
    shift

    # Prepare additional data
    local data=
    if [[ $# -gt 0 ]]; then
        for item in "$@"; do
            if [[ "${item}" == *=* ]]; then
                data+="\033[1m\033[38;5;236m${item%%=*}=\033[0m\"${item#*=}\" "
            else
                data+="${item} "
            fi
        done
    fi

    # Determine output stream based on log level
    local output_stream="/dev/stdout"
    if [[ "$level" == "error" ]]; then
        output_stream="/dev/stderr"
    fi

    # Print the log message
    local level_upper
    level_upper=$(printf '%s' "$level" | tr '[:lower:]' '[:upper:]')
    printf "%s %b%s%b %s %b\n" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
        "${color}" "${level_upper}" "\033[0m" "${msg}" "${data}" >"${output_stream}"

    # Exit if the log level is error
    if [[ "$level" == "error" ]]; then
        exit 1
    fi
}

# Check if required environment variables are set
function check_env() {
    local envs=("${@}")
    local missing=()
    local values=()

    for env in "${envs[@]}"; do
        if [[ -z "${!env-}" ]]; then
            missing+=("${env}")
        else
            values+=("${env}=${!env}")
        fi
    done

    if [ ${#missing[@]} -ne 0 ]; then
        log error "Missing required env variables" "envs=${missing[*]}"
    fi

    log debug "Env variables are set" "envs=${values[*]}"
}

# Check if required CLI tools are installed
function check_cli() {
    local deps=("${@}")
    local missing=()

    for dep in "${deps[@]}"; do
        if ! command -v "${dep}" &>/dev/null; then
            missing+=("${dep}")
        fi
    done

    if [ ${#missing[@]} -ne 0 ]; then
        log error "Missing required deps" "deps=${missing[*]}"
    fi

    log debug "Deps are installed" "deps=${deps[*]}"
}
