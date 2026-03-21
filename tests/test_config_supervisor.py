# tests/test_config_supervisor.py
"""Tests for SupervisorConfig and ReflectionConfig."""


def test_reflection_config_defaults():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig()
    assert cfg.level == "full"
    assert cfg.periodic_interval == 900
    assert cfg.max_depth == 3
    assert cfg.per_cycle_token_cap == 10000
    assert cfg.hourly_token_circuit_breaker == 100000


def test_reflection_config_validation_valid():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="moderate", max_depth=2)
    errors = cfg.validate()
    assert len(errors) == 0


def test_reflection_config_validation_invalid_level():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="turbo")
    errors = cfg.validate()
    assert any("level" in str(e) for e in errors)


def test_reflection_config_validation_invalid_depth():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(max_depth=0)
    errors = cfg.validate()
    assert any("max_depth" in str(e) for e in errors)


def test_supervisor_config_defaults():
    from src.config import SupervisorConfig
    cfg = SupervisorConfig()
    assert cfg.reflection is not None
    assert cfg.reflection.level == "full"


def test_supervisor_config_in_app_config():
    from src.config import AppConfig, SupervisorConfig
    app = AppConfig()
    assert hasattr(app, "supervisor")
    assert isinstance(app.supervisor, SupervisorConfig)


def test_reflection_config_off_disables():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="off")
    errors = cfg.validate()
    assert len(errors) == 0


def test_supervisor_config_validation():
    from src.config import SupervisorConfig
    cfg = SupervisorConfig()
    errors = cfg.validate()
    assert len(errors) == 0
