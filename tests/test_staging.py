"""Tests for staging config fields on DreamConfig."""

from nanobot.config.schema import DreamConfig


def test_default_staging_promotion_threshold():
    cfg = DreamConfig()
    assert cfg.staging_promotion_threshold == 3


def test_default_audit_cron():
    cfg = DreamConfig()
    assert cfg.audit_cron == "0 3 * * *"


def test_default_audit_model_override_is_none():
    cfg = DreamConfig()
    assert cfg.audit_model_override is None


def test_default_audit_max_iterations():
    cfg = DreamConfig()
    assert cfg.audit_max_iterations == 15


def test_build_audit_schedule():
    cfg = DreamConfig()
    schedule = cfg.build_audit_schedule("UTC")
    assert schedule.kind == "cron"
    assert schedule.expr == "0 3 * * *"
    assert schedule.tz == "UTC"


def test_custom_audit_cron():
    cfg = DreamConfig(audit_cron="0 5 * * *")
    schedule = cfg.build_audit_schedule("Asia/Shanghai")
    assert schedule.expr == "0 5 * * *"
    assert schedule.tz == "Asia/Shanghai"


def test_camel_case_aliases():
    """Config JSON uses camelCase; verify aliases resolve."""
    cfg = DreamConfig(stagingPromotionThreshold=5, auditMaxIterations=20)
    assert cfg.staging_promotion_threshold == 5
    assert cfg.audit_max_iterations == 20
