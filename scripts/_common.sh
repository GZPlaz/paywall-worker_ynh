#!/bin/bash

#=================================================
# COMMON VARIABLES AND CUSTOM HELPERS
#=================================================

# shellcheck disable=SC2034  # used by the scripts sourcing this file
log_file="/var/log/$app/$app.log"

# Copy the application code shipped in this package to the install dir
_install_sources() {
    mkdir -p "$install_dir/app"
    rm -rf "$install_dir/app/paywall" "$install_dir/app/worker"
    cp -a "$YNH_APP_BASEDIR/src/paywall" "$install_dir/app/paywall"
    cp -a "$YNH_APP_BASEDIR/src/worker" "$install_dir/app/worker"
    cp "$YNH_APP_BASEDIR/src/requirements.txt" "$install_dir/app/requirements.txt"
}

# Create (or repair) the Python virtualenv and install the dependencies.
# Everything runs as the app user, which owns $install_dir.
_as_app() {
    (cd "$install_dir" && sudo -u "$app" env HOME="$install_dir" PIP_NO_CACHE_DIR=1 "$@")
}

_install_python_deps() {
    if [ -d "$install_dir/venv" ] && ! _as_app "$install_dir/venv/bin/python" -c "pass" >/dev/null 2>&1; then
        # Python was upgraded since the venv was made (e.g. restore on a newer system)
        ynh_safe_rm "$install_dir/venv"
    fi
    if [ ! -x "$install_dir/venv/bin/python" ]; then
        _as_app python3 -m venv "$install_dir/venv"
    fi
    _as_app "$install_dir/venv/bin/pip" install --quiet --upgrade pip
    _as_app "$install_dir/venv/bin/pip" install --quiet --requirement "$install_dir/app/requirements.txt"
}

# Run the paywall CLI as the app user
_paywall_cli() {
    (cd "$install_dir/app" && sudo -u "$app" env PAYWALL_CONFIG="$install_dir/config.json" PYTHONPATH="$install_dir/app" \
        "$install_dir/venv/bin/python" -m paywall "$@")
}

# Store a default value for every setting that is not set yet
_set_default_settings() {
    local key value
    while IFS= read -r -d '' key && IFS= read -r -d '' value; do
        if [ -z "$(ynh_app_setting_get --key="$key")" ]; then
            ynh_app_setting_set --key="$key" --value="$value"
        fi
    done < <(_paywall_cli config-defaults)

    for key in signing_secret flask_secret origin_secret; do
        if [ -z "$(ynh_app_setting_get --key="$key")" ]; then
            ynh_app_setting_set --key="$key" --value="$(ynh_string_random --length=48)"
        fi
    done
}

# Render $install_dir/config.json from the app settings.
# Values are piped NUL-separated so they may contain any character.
_write_config() {
    local key
    {
        printf '%s\0%s\0' app "$app" data_dir "$data_dir"
        for key in $(_paywall_cli config-keys); do
            printf '%s\0%s\0' "$key" "$(ynh_app_setting_get --key="$key")"
        done
        # During change_url the settings still hold the old values
        if [ -n "${new_domain:-}" ]; then
            printf '%s\0%s\0' domain "$new_domain" path "$new_path"
        fi
    } | _paywall_cli write-config "$install_dir/config.json"
}

_set_permissions() {
    chown -R "$app:$app" "$install_dir"
    chmod -R o-rwx "$install_dir"
    chmod 750 "$install_dir"
    chown -R "$app:$app" "$data_dir"
    chmod 750 "$data_dir"
    if [ -f "$install_dir/paywall-cli" ]; then
        chown root:root "$install_dir/paywall-cli"
        chmod 700 "$install_dir/paywall-cli"
    fi
    mkdir -p "/var/log/$app"
    chown -R "$app:$app" "/var/log/$app"
}

_install_cli() {
    ynh_config_add --template="paywall-cli" --destination="$install_dir/paywall-cli"
}

# Push the Worker to Cloudflare if the admin enabled automatic deployment
_auto_deploy_worker() {
    if [ "$(ynh_app_setting_get --key=cf_auto_deploy)" == "1" ] \
        && [ -n "$(ynh_app_setting_get --key=cf_api_token)" ]; then
        ynh_script_progression "Updating the Cloudflare Worker..."
        _paywall_cli deploy-worker --if-configured \
            || ynh_print_warn "The Cloudflare Worker could not be updated. Check the settings in the config panel and use the 'Deploy' button."
    fi
}
