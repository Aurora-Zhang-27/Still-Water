from settings import PROJECT_ROOT, load_settings


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_placeholders_count_as_unset(tmp_path):
    env = write_env(
        tmp_path,
        "# a comment\n"
        "IBKR_FLEX_TOKEN=your_flex_token_here\n"
        "IB_GATEWAY_PORT=your_gateway_port_here\n"
        "DB_PATH=data/x.db\n",
    )
    s = load_settings(env, environ={})
    assert s.flex_token is None
    assert s.gateway_port is None
    assert s.db_path == PROJECT_ROOT / "data/x.db"
    assert s.rules_path == PROJECT_ROOT / "rules.yaml"


def test_environment_wins_over_file(tmp_path):
    env = write_env(tmp_path, "IBKR_FLEX_QUERY_ID_ACTIVITY=111\n")
    s = load_settings(env, environ={"IBKR_FLEX_QUERY_ID_ACTIVITY": "222", "IB_GATEWAY_PORT": "4002"})
    assert s.flex_query_id_activity == "222"
    assert s.gateway_port == 4002


def test_quotes_and_inline_comments(tmp_path):
    env = write_env(tmp_path, 'IBKR_FLEX_QUERY_ID_ACTIVITY="123"\nIB_CLIENT_ID=7  # whatever\n')
    s = load_settings(env, environ={})
    assert s.flex_query_id_activity == "123"
    assert s.gateway_client_id == 7


def test_secrets_stay_out_of_repr(tmp_path):
    env = write_env(tmp_path, "IBKR_FLEX_TOKEN=tok_abc123\nIBKR_ACCOUNT_ID=U7654321\n")
    s = load_settings(env, environ={})
    assert s.flex_token == "tok_abc123"
    assert "tok_abc123" not in repr(s)
    assert "U7654321" not in repr(s)
