import pytest

from careerpilot.config import load_settings
from careerpilot.cost_guard import CostPolicyViolation, Integration, load_integrations, validate_startup
from careerpilot.matching.model import NullModel, get_model


def test_default_configuration_is_zero_cost(settings):
    checks = validate_startup(settings)
    assert "ZERO_COST_MODE=true" in checks
    assert settings.dry_run is True
    assert settings.submissions_kill_switch is True
    assert settings.real_submissions_enabled is False


def test_zero_cost_mode_cannot_be_disabled(make_settings):
    with pytest.raises(CostPolicyViolation):
        validate_startup(make_settings(zero_cost_mode=False))


def test_paid_llm_provider_is_rejected(make_settings):
    with pytest.raises(CostPolicyViolation):
        validate_startup(make_settings(llm_provider="openai"))
    with pytest.raises(CostPolicyViolation):
        get_model(make_settings(llm_provider="anthropic"))


def test_local_llm_must_be_localhost(make_settings):
    validate_startup(make_settings(llm_provider="ollama", llm_base_url="http://localhost:11434"))
    with pytest.raises(CostPolicyViolation):
        validate_startup(make_settings(llm_provider="ollama", llm_base_url="https://hosted.example.com"))


def test_enabling_a_paid_integration_blocks_startup(settings):
    items = load_integrations(settings.config_dir)
    paid = [Integration("premium_board", "source", True, "paid", "subscription")]
    with pytest.raises(CostPolicyViolation, match="premium_board"):
        validate_startup(settings, items + paid)
    trial = [Integration("trial_service", "source", True, "trial", "auto-converts")]
    with pytest.raises(CostPolicyViolation):
        validate_startup(settings, items + trial)


def test_sources_that_forbid_automation_cannot_be_enabled(settings):
    items = [Integration("linkedin_jobs", "source", True, "free", "", automation_permitted=False)]
    with pytest.raises(CostPolicyViolation, match="linkedin_jobs"):
        validate_startup(settings, items)


def test_bundled_inventory_has_no_enabled_paid_items(settings):
    for item in load_integrations(settings.config_dir):
        if item.enabled:
            assert item.cost == "free", item.name


def test_default_model_is_null(settings):
    assert isinstance(get_model(settings), NullModel)


def test_env_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("APPROVED_SUBMISSION_DOMAINS", "A.example, b.example")
    s = load_settings(env_file=tmp_path / "none.env")
    assert s.dry_run is False
    assert s.approved_submission_domains == ["a.example", "b.example"]
    assert s.submissions_kill_switch is True  # default stays safe
