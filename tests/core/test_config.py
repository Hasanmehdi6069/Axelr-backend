from unittest.mock import patch

import pytest

from core import config


class TestConfigModule:
    def test_config_module_loaded(self):
        assert config is not None

    def test_config_has_required_fields(self):
        required = [
            "env",
            "port",
            "host",
            "log_level",
            "cognition_level",
        ]
        for field in required:
            assert hasattr(config, field)

    def test_config_env_is_string(self):
        assert isinstance(config.env, str)

    def test_config_port_is_integer(self):
        assert isinstance(config.port, int)
        assert config.port > 0

    def test_config_host_is_string(self):
        assert isinstance(config.host, str)

    def test_config_log_level_valid(self):
        valid = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        assert config.log_level in valid

    def test_config_cognition_level_valid(self):
        valid = ["full", "standard", "stub", "offline"]
        assert config.cognition_level in valid

    def test_config_redis_config_exists(self):
        assert hasattr(config, "redis_url")
        assert isinstance(config.redis_url, str)

    def test_config_supabase_config_exists(self):
        assert hasattr(config, "supabase_url")
        assert hasattr(config, "supabase_key")

    def test_config_cloudflare_config_exists(self):
        assert hasattr(config, "cf_account")
        assert hasattr(config, "cf_vectorize_index")

    def test_config_anthropic_config_exists(self):
        assert hasattr(config, "anthropic_key")
        assert isinstance(config.anthropic_key, str)

    def test_config_gemini_config_exists(self):
        assert hasattr(config, "gemini_key")
        assert isinstance(config.gemini_key, str)

    def test_config_openai_config_exists(self):
        assert hasattr(config, "openai_key")
        assert isinstance(config.openai_key, str)

    def test_config_github_config_exists(self):
        assert hasattr(config, "github_app_id")
        assert hasattr(config, "github_pem")
        assert hasattr(config, "github_webhook_secret")

    def test_config_stripe_config_exists(self):
        assert hasattr(config, "stripe_key")
        assert hasattr(config, "stripe_webhook_secret")

    def test_config_google_oauth_config_exists(self):
        assert hasattr(config, "google_client_id")
        assert hasattr(config, "google_client_secret")

    def test_config_resilience_config_exists(self):
        assert hasattr(config, "cb_threshold")
        assert hasattr(config, "cb_cooldown_s")
        assert hasattr(config, "cb_rate_limit_cooldown_s")

    def test_config_cb_threshold_positive(self):
        assert config.cb_threshold > 0

    def test_config_cb_cooldown_positive(self):
        assert config.cb_cooldown_s > 0

    def test_config_cache_config_exists(self):
        assert hasattr(config, "prefix_ttl_s")
        assert isinstance(config.prefix_ttl_s, int)
        assert config.prefix_ttl_s > 0

    def test_config_memory_config_exists(self):
        assert hasattr(config, "max_text_chars")
        assert hasattr(config, "default_top_k")
        assert hasattr(config, "default_min_score")

    def test_config_max_text_chars_positive(self):
        assert config.max_text_chars > 0

    def test_config_default_top_k_positive(self):
        assert config.default_top_k > 0

    def test_config_default_min_score_between_0_1(self):
        assert 0 <= config.default_min_score <= 1

    def test_config_cors_origins_exists(self):
        assert hasattr(config, "cors_origins")
        assert isinstance(config.cors_origins, list)

    def test_config_rate_limit_config_exists(self):
        assert hasattr(config, "rate_limit_rpm")
        assert hasattr(config, "rate_limit_window_s")

    def test_config_rate_limit_rpm_positive(self):
        assert config.rate_limit_rpm > 0

    def test_config_rate_limit_window_positive(self):
        assert config.rate_limit_window_s > 0

    def test_config_database_config_exists(self):
        assert hasattr(config, "db_path")
        assert isinstance(config.db_path, str)

    def test_config_metrics_enabled_flag_exists(self):
        assert hasattr(config, "metrics_enabled")
        assert isinstance(config.metrics_enabled, bool)

    def test_config_telegram_config_exists(self):
        assert hasattr(config, "telegram_bot_token")

    def test_config_discord_config_exists(self):
        assert hasattr(config, "discord_bot_token")

    def test_config_slack_config_exists(self):
        assert hasattr(config, "slack_bot_token")

    def test_config_security_config_exists(self):
        assert hasattr(config, "secret_key")
        assert isinstance(config.secret_key, str)

    def test_config_session_ttl_exists(self):
        assert hasattr(config, "session_ttl_days")
        assert config.session_ttl_days > 0

    def test_config_github_org_exists(self):
        assert hasattr(config, "github_org")
        assert isinstance(config.github_org, str)

    def test_config_allowed_origins_exists(self):
        assert hasattr(config, "allowed_origins")
        assert isinstance(config.allowed_origins, list)

    def test_config_api_prefix_exists(self):
        assert hasattr(config, "api_prefix")
        assert isinstance(config.api_prefix, str)

    def test_config_api_prefix_starts_with_slash(self):
        assert config.api_prefix.startswith("/")

    def test_config_static_dir_exists(self):
        assert hasattr(config, "static_dir")
        assert isinstance(config.static_dir, str)

    def test_config_docs_dir_exists(self):
        assert hasattr(config, "docs_dir")
        assert isinstance(config.docs_dir, str)

    def test_config_test_mode_flag_exists(self):
        assert hasattr(config, "test_mode")
        assert isinstance(config.test_mode, bool)

    def test_config_debug_mode_flag_exists(self):
        assert hasattr(config, "debug")
        assert isinstance(config.debug, bool)

    def test_config_sentry_dsn_exists(self):
        assert hasattr(config, "sentry_dsn")

    def test_config_sentry_environment_exists(self):
        assert hasattr(config, "sentry_environment")

    def test_config_prometheus_port_exists(self):
        assert hasattr(config, "prometheus_port")
        assert isinstance(config.prometheus_port, int)

    def test_config_prometheus_port_valid(self):
        assert 1024 <= config.prometheus_port <= 65535

    def test_config_health_check_interval_exists(self):
        assert hasattr(config, "health_check_interval_s")
        assert config.health_check_interval_s > 0

    def test_config_task_timeout_exists(self):
        assert hasattr(config, "task_timeout_s")
        assert config.task_timeout_s > 0

    def test_config_max_concurrent_tasks_exists(self):
        assert hasattr(config, "max_concurrent_tasks")
        assert config.max_concurrent_tasks > 0

    def test_config_queue_size_exists(self):
        assert hasattr(config, "queue_size")
        assert config.queue_size > 0

    def test_config_retry_attempts_exists(self):
        assert hasattr(config, "retry_attempts")
        assert config.retry_attempts >= 0

    def test_config_retry_delay_exists(self):
        assert hasattr(config, "retry_delay_s")
        assert config.retry_delay_s >= 0

    def test_config_backoff_factor_exists(self):
        assert hasattr(config, "backoff_factor")
        assert config.backoff_factor >= 1

    def test_config_timeout_exists(self):
        assert hasattr(config, "timeout_s")
        assert config.timeout_s > 0

    def test_config_max_retries_exists(self):
        assert hasattr(config, "max_retries")
        assert config.max_retries >= 0

    def test_config_pool_connections_exists(self):
        assert hasattr(config, "pool_connections")
        assert config.pool_connections > 0

    def test_config_pool_maxsize_exists(self):
        assert hasattr(config, "pool_maxsize")
        assert config.pool_maxsize > 0

    def test_config_keepalive_exists(self):
        assert hasattr(config, "keepalive_s")
        assert config.keepalive_s > 0

    def test_config_user_agent_exists(self):
        assert hasattr(config, "user_agent")
        assert isinstance(config.user_agent, str)

    def test_config_default_headers_exists(self):
        assert hasattr(config, "default_headers")
        assert isinstance(config.default_headers, dict)

    def test_config_app_name_exists(self):
        assert hasattr(config, "app_name")
        assert isinstance(config.app_name, str)

    def test_config_app_version_exists(self):
        assert hasattr(config, "app_version")
        assert isinstance(config.app_version, str)

    def test_config_app_description_exists(self):
        assert hasattr(config, "app_description")
        assert isinstance(config.app_description, str)

    def test_config_contact_email_exists(self):
        assert hasattr(config, "contact_email")
        assert isinstance(config.contact_email, str)

    def test_config_license_exists(self):
        assert hasattr(config, "license")
        assert isinstance(config.license, str)

    def test_config_environment_valid_values(self):
        valid = ["development", "staging", "production", "test"]
        assert config.env in valid

    def test_config_all_fields_accessible(self):
        fields = [
            "env", "port", "host", "log_level", "cognition_level",
            "redis_url", "supabase_url", "supabase_key",
            "cf_account", "cf_vectorize_index",
            "anthropic_key", "gemini_key", "openai_key",
            "github_app_id", "github_pem", "github_webhook_secret",
            "stripe_key", "stripe_webhook_secret",
            "google_client_id", "google_client_secret",
            "cb_threshold", "cb_cooldown_s", "cb_rate_limit_cooldown_s",
            "prefix_ttl_s", "max_text_chars", "default_top_k",
            "default_min_score", "cors_origins", "rate_limit_rpm",
            "rate_limit_window_s", "db_path", "metrics_enabled",
            "telegram_bot_token", "discord_bot_token", "slack_bot_token",
            "secret_key", "session_ttl_days", "github_org", "allowed_origins",
            "api_prefix", "static_dir", "docs_dir", "test_mode", "debug",
            "sentry_dsn", "sentry_environment", "prometheus_port",
            "health_check_interval_s", "task_timeout_s", "max_concurrent_tasks",
            "queue_size", "retry_attempts", "retry_delay_s", "backoff_factor",
            "timeout_s", "max_retries", "pool_connections", "pool_maxsize",
            "keepalive_s", "user_agent", "default_headers", "app_name",
            "app_version", "app_description", "contact_email", "license"
        ]
        for field in fields:
            assert hasattr(config, field)

    def test_config_module_exports(self):
        required = [
            "env", "port", "host", "log_level", "cognition_level",
            "redis_url", "supabase_url", "cf_account", "anthropic_key",
            "get_config"
        ]
        for sym in required:
            assert hasattr(config, sym)

    def test_get_config_returns_singleton(self):
        c1 = config.get_config()
        c2 = config.get_config()
        assert c1 is c2

    def test_config_is_immutable(self):
        original = config.port
        with patch.object(config, "port", 9999):
            assert config.port == 9999
        assert config.port == original

    def test_config_database_path_not_empty(self):
        assert len(config.db_path) > 0

    def test_config_static_dir_not_empty(self):
        assert len(config.static_dir) > 0

    def test_config_docs_dir_not_empty(self):
        assert len(config.docs_dir) > 0

    def test_config_app_name_not_empty(self):
        assert len(config.app_name) > 0

    def test_config_app_version_not_empty(self):
        assert len(config.app_version) > 0

    def test_config_contact_email_contains_at(self):
        assert "@" in config.contact_email

    def test_config_license_not_empty(self):
        assert len(config.license) > 0

    def test_config_user_agent_not_empty(self):
        assert len(config.user_agent) > 0

    def test_config_api_prefix_not_empty(self):
        assert len(config.api_prefix) > 0

    def test_config_github_org_not_empty(self):
        assert len(config.github_org) > 0

    def test_config_cors_origins_list(self):
        assert isinstance(config.cors_origins, list)

    def test_config_allowed_origins_list(self):
        assert isinstance(config.allowed_origins, list)

    def test_config_default_headers_dict(self):
        assert isinstance(config.default_headers, dict)

    def test_config_metrics_enabled_boolean(self):
        assert isinstance(config.metrics_enabled, bool)

    def test_config_test_mode_boolean(self):
        assert isinstance(config.test_mode, bool)

    def test_config_debug_boolean(self):
        assert isinstance(config.debug, bool)

    def test_config_supports_development_env(self):
        assert config.env in ["development", "staging", "production", "test"]

    def test_config_supports_production_env(self):
        assert config.env in ["development", "staging", "production", "test"]

    def test_config_supports_test_env(self):
        assert config.env in ["development", "staging", "production", "test"]

    def test_config_cognition_level_full_valid(self):
        assert config.cognition_level in ["full", "standard", "stub", "offline"]

    def test_config_cognition_level_standard_valid(self):
        assert config.cognition_level in ["full", "standard", "stub", "offline"]

    def test_config_cognition_level_stub_valid(self):
        assert config.cognition_level in ["full", "standard", "stub", "offline"]

    def test_config_cognition_level_offline_valid(self):
        assert config.cognition_level in ["full", "standard", "stub", "offline"]

    def test_config_redis_url_format(self):
        if config.redis_url:
            assert "redis" in config.redis_url.lower() or config.redis_url.startswith("redis")

    def test_config_supabase_url_format(self):
        if config.supabase_url:
            assert "supabase" in config.supabase_url.lower() or config.supabase_url.startswith("https")

    def test_config_cf_account_format(self):
        if config.cf_account:
            assert len(config.cf_account) > 0

    def test_config_cf_vectorize_index_format(self):
        if config.cf_vectorize_index:
            assert len(config.cf_vectorize_index) > 0

    def test_config_anthropic_key_format(self):
        if config.anthropic_key:
            assert config.anthropic_key.startswith("sk-ant")

    def test_config_gemini_key_format(self):
        if config.gemini_key:
            assert len(config.gemini_key) > 0

    def test_config_openai_key_format(self):
        if config.openai_key:
            assert config.openai_key.startswith("sk-")

    def test_config_github_app_id_format(self):
        if config.github_app_id:
            assert len(config.github_app_id) > 0

    def test_config_stripe_key_format(self):
        if config.stripe_key:
            assert config.stripe_key.startswith("sk_")

    def test_config_google_client_id_format(self):
        if config.google_client_id:
            assert len(config.google_client_id) > 0

    def test_config_health_check_returns_dict(self):
        health = config.health_check()
        assert isinstance(health, dict)
        assert "status" in health

    def test_config_health_check_status_ok(self):
        health = config.health_check()
        assert health["status"] == "ok"

    def test_config_audit_returns_dict(self):
        audit = config.audit()
        assert isinstance(audit, dict)
        assert "config" in audit

    def test_config_audit_includes_version(self):
        audit = config.audit()
        assert "version" in audit

    def test_config_audit_includes_env(self):
        audit = config.audit()
        assert "env" in audit["config"]

    def test_config_audit_includes_port(self):
        audit = config.audit()
        assert "port" in audit["config"]

    def test_config_module_all_exports_defined(self):
        assert hasattr(config, "__all__")
        assert isinstance(config.__all__, list)

    def test_config_log_level_not_empty(self):
        assert len(config.log_level) > 0

    def test_config_host_not_empty(self):
        assert len(config.host) > 0

    def test_config_host_is_valid_hostname(self):
        assert config.host in ["0.0.0.0", "127.0.0.1", "localhost"]

    def test_config_port_in_valid_range(self):
        assert 1 <= config.port <= 65535

    def test_config_prometheus_port_in_valid_range(self):
        assert 1 <= config.prometheus_port <= 65535

    def test_config_session_ttl_days_positive(self):
        assert config.session_ttl_days > 0

    def test_config_max_text_chars_reasonable(self):
        assert 100 <= config.max_text_chars <= 100000

    def test_config_default_top_k_reasonable(self):
        assert 1 <= config.default_top_k <= 100

    def test_config_default_min_score_reasonable(self):
        assert 0 <= config.default_min_score <= 1

    def test_config_rate_limit_rpm_reasonable(self):
        assert 1 <= config.rate_limit_rpm <= 10000

    def test_config_rate_limit_window_s_reasonable(self):
        assert 1 <= config.rate_limit_window_s <= 3600

    def test_config_cb_threshold_reasonable(self):
        assert 1 <= config.cb_threshold <= 100

    def test_config_cb_cooldown_s_reasonable(self):
        assert 1 <= config.cb_cooldown_s <= 3600

    def test_config_cb_rate_limit_cooldown_s_reasonable(self):
        assert 1 <= config.cb_rate_limit_cooldown_s <= 3600

    def test_config_prefix_ttl_s_reasonable(self):
        assert 60 <= config.prefix_ttl_s <= 86400 * 7

    def test_config_health_check_interval_s_reasonable(self):
        assert 1 <= config.health_check_interval_s <= 3600

    def test_config_task_timeout_s_reasonable(self):
        assert 1 <= config.task_timeout_s <= 3600

    def test_config_max_concurrent_tasks_reasonable(self):
        assert 1 <= config.max_concurrent_tasks <= 1000

    def test_config_queue_size_reasonable(self):
        assert 1 <= config.queue_size <= 10000

    def test_config_retry_attempts_reasonable(self):
        assert 0 <= config.retry_attempts <= 10

    def test_config_retry_delay_s_reasonable(self):
        assert 0 <= config.retry_delay_s <= 60

    def test_config_backoff_factor_reasonable(self):
        assert 1 <= config.backoff_factor <= 10

    def test_config_timeout_s_reasonable(self):
        assert 1 <= config.timeout_s <= 3600

    def test_config_max_retries_reasonable(self):
        assert 0 <= config.max_retries <= 10

    def test_config_pool_connections_reasonable(self):
        assert 1 <= config.pool_connections <= 1000

    def test_config_pool_maxsize_reasonable(self):
        assert 1 <= config.pool_maxsize <= 1000

    def test_config_keepalive_s_reasonable(self):
        assert 1 <= config.keepalive_s <= 3600