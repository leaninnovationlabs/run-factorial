from runtime.adapters._claude.credentials import (
    ENV_API_KEY,
    ENV_OAUTH_TOKEN,
    credential_environment,
)


def test_subscription_drops_a_host_api_key():
    env = credential_environment(
        {ENV_API_KEY: "host-key", "PATH": "/bin"}, auth_mode="subscription", credential="oauth"
    )
    assert ENV_API_KEY not in env and env[ENV_OAUTH_TOKEN] == "oauth" and env["PATH"] == "/bin"


def test_api_key_overrides_the_host_key_and_drops_a_token():
    env = credential_environment(
        {ENV_API_KEY: "host-key", ENV_OAUTH_TOKEN: "stale"},
        auth_mode="api_key",
        credential="stored-key",
    )
    assert env[ENV_API_KEY] == "stored-key" and ENV_OAUTH_TOKEN not in env
