#!/bin/sh
# SECURITY: refuse a CSP_HEADER_NAME or CSP_CONNECT_SRC_API value that would weaken or remove
# the container's Content-Security-Policy.
#
# nginx.conf is a TEMPLATE and these two variables are substituted into an `add_header`
# directive. Without this gate, two operator values start the container with a policy that
# protects less than the configuration says it does, and nginx reports no error either time:
#
#   CSP_HEADER_NAME=X-Not-A-CSP           the add_header emits a header no browser treats as a
#                                         policy, so the CSP disappears and five of the six
#                                         canonical headers remain.
#   CSP_CONNECT_SRC_API='... "; add_header X-Evil 1; #'
#                                         the closing quote and semicolon end the add_header
#                                         directive early, truncating the policy - dropping
#                                         upgrade-insecure-requests - and appending an
#                                         attacker-chosen header of its own.
#
# A CRLF in CSP_HEADER_NAME already fails closed, because nginx itself refuses the resulting
# directive. This script makes every other hostile value behave the same way, and applies the
# same origin grammar infrastructure/terraform/variables.tf enforces on var.api_origin, so a
# value accepted here is accepted there.
#
# Installed as /docker-entrypoint.d/15-csp-env-validate.sh: the nginx entrypoint runs that
# directory in lexical order under `set -e`, and 20-envsubst-on-templates.sh does the
# substitution, so this runs BEFORE the template is rendered and a non-zero exit stops the
# container before it ever serves a request.
set -eu

ENTRYPOINT_TAG="csp-env-validate"

refuse() {
    echo "$ENTRYPOINT_TAG: REFUSING TO START. $1" >&2
    shift
    while [ "$#" -gt 0 ]; do
        echo "  $1" >&2
        shift
    done
    exit 1
}

# Only the two policy header names are legal. Anything else silently removes the policy,
# because the directive would emit a header that is not a Content-Security-Policy at all.
case "${CSP_HEADER_NAME:-}" in
    Content-Security-Policy | Content-Security-Policy-Report-Only) ;;
    *)
        refuse "CSP_HEADER_NAME is '${CSP_HEADER_NAME:-<unset>}'." \
            "It must be exactly Content-Security-Policy, or Content-Security-Policy-Report-Only" \
            "to report without blocking. Any other value emits a header no browser enforces, so" \
            "the policy would be absent while the header count still looked correct."
        ;;
esac

# The API origin is optional in form but required in practice, and it carries ONE leading space
# because it is concatenated onto the connect-src source list. Everything after that single
# space must be one bare https origin: scheme, host, optional port, and nothing else - no path,
# no query, no userinfo, no wildcard, no second token, and no character that could terminate
# the add_header directive it is substituted into.
API_SOURCE="${CSP_CONNECT_SRC_API:-}"
if [ -n "$API_SOURCE" ]; then
    case "$API_SOURCE" in
        " "*) ;;
        *)
            refuse "CSP_CONNECT_SRC_API does not begin with a single space." \
                "It is concatenated onto the connect-src source list, so it must read" \
                "\" https://api.example.com\" - one leading space, then the origin." \
                "Observed: '$API_SOURCE'"
            ;;
    esac
    API_ORIGIN="${API_SOURCE# }"

    case "$API_ORIGIN" in
        " "*)
            refuse "CSP_CONNECT_SRC_API begins with more than one space." \
                "Exactly one leading space, then a single origin. Observed: '$API_SOURCE'"
            ;;
    esac

    # A second whitespace-separated token would admit an extra source that no configuration
    # names, so the value is required to be one token.
    case "$API_ORIGIN" in
        *[[:space:]]*)
            refuse "CSP_CONNECT_SRC_API names more than one source." \
                "Exactly one origin is admitted; this container serves static files only and has" \
                "one API to reach. Observed: '$API_SOURCE'"
            ;;
    esac

    # Anything that could end the add_header directive or start another one.
    case "$API_ORIGIN" in
        *'"'* | *"'"* | *';'* | *'#'* | *'\'* | *'$'* | *'{'* | *'}'*)
            refuse "CSP_CONNECT_SRC_API contains a character that can terminate the nginx directive it is substituted into." \
                "A quote, semicolon, backslash, dollar, brace or comment character would end the" \
                "add_header early - truncating the policy - and could append a header of its own." \
                "Observed: '$API_SOURCE'"
            ;;
    esac

    # The same grammar variables.tf applies to var.api_origin: https only, a lower-case host of
    # dot-separated alphanumeric labels, an optional port, and no path.
    case "$API_ORIGIN" in
        https://*) ;;
        *)
            refuse "CSP_CONNECT_SRC_API is not an https origin." \
                "Plaintext exposes every request and its bearer token to any network position, and" \
                "a wildcard admits every host. Observed: '$API_SOURCE'"
            ;;
    esac
    API_AUTHORITY="${API_ORIGIN#https://}"
    case "$API_AUTHORITY" in
        */* | *@* | *'?'* | *'*'*)
            refuse "CSP_CONNECT_SRC_API is not a bare origin." \
                "connect-src matches an origin, so a path, query, userinfo or wildcard makes the" \
                "source either useless or far wider than intended. Observed: '$API_SOURCE'"
            ;;
    esac
    API_HOST="${API_AUTHORITY%%:*}"
    if [ "$API_HOST" != "$API_AUTHORITY" ]; then
        API_PORT="${API_AUTHORITY#*:}"
        case "$API_PORT" in
            "" | *[!0-9]*)
                refuse "CSP_CONNECT_SRC_API has the non-numeric port '$API_PORT'." \
                    "Observed: '$API_SOURCE'"
                ;;
        esac
        if [ "$API_PORT" -lt 1 ] || [ "$API_PORT" -gt 65535 ]; then
            refuse "CSP_CONNECT_SRC_API has the out-of-range port '$API_PORT'." \
                "A port is 1 to 65535. Observed: '$API_SOURCE'"
        fi
    fi
    if [ -z "$API_HOST" ]; then
        refuse "CSP_CONNECT_SRC_API names no host." "Observed: '$API_SOURCE'"
    fi
    # Lower-case labels of alphanumerics and hyphens, each starting and ending alphanumeric,
    # separated by single dots. `expr` is used rather than `grep -E` so the check needs nothing
    # beyond the shell's own utilities.
    if ! expr "$API_HOST" : '[0-9a-z]\([0-9a-z-]*[0-9a-z]\)\{0,1\}\(\.[0-9a-z]\([0-9a-z-]*[0-9a-z]\)\{0,1\}\)*$' >/dev/null; then
        refuse "CSP_CONNECT_SRC_API host '$API_HOST' is not a lower-case ASCII hostname." \
            "Labels are alphanumerics and hyphens, start and end alphanumeric, and are separated" \
            "by single dots. An upper-case, unicode, empty-label or trailing-dot host is refused" \
            "because a browser matches the source literally, so it would admit nothing while" \
            "appearing configured. Observed: '$API_SOURCE'"
    fi
    echo "$ENTRYPOINT_TAG: policy header '$CSP_HEADER_NAME', connect-src API origin '$API_ORIGIN'."
else
    # Empty is a legal value and the image's own default. It renders a policy that admits no API
    # origin, which is safe but non-functional, so it is announced rather than passed silently.
    echo "$ENTRYPOINT_TAG: policy header '$CSP_HEADER_NAME', no API origin set." >&2
    echo "  CSP_CONNECT_SRC_API is empty, so connect-src admits no API and a browser enforcing" >&2
    echo "  the rendered policy will block every API call this application makes. Set it to a" >&2
    echo "  single leading space followed by the API origin." >&2
fi
