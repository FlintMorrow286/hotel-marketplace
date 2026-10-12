import pytest
from cryptography.fernet import Fernet

from app.config.checks import production_problems
from app.config.settings import Settings


def _production_settings(app_base_url: str) -> Settings:
    return Settings(
        app_env="production",
        app_base_url=app_base_url,
        jwt_secret="review-secret-" + "x" * 32,
        allow_private_targets=False,
        webhook_seal_key=Fernet.generate_key().decode(),
        payment_mode="stub",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.2",
        "https://10.0.0.1",
        "https://169.254.169.254",
        "https://0.0.0.0",
        "https://240.0.0.1",
        "https://[::1]",
        "https://localhost",
        "https://localhost.",
        "https://api.localhost",
        "https://service.local",
        "https://db.internal",
        "https://",
    ],
)
def test_production_rejects_nonpublic_base_url_hosts(url: str) -> None:
    problems = production_problems(_production_settings(url))

    assert any(
        problem.startswith("APP_BASE_URL must be the public https address") for problem in problems
    )


def test_production_accepts_a_public_hostname() -> None:
    problems = production_problems(_production_settings("https://partners.example.com/"))

    assert not any(problem.startswith("APP_BASE_URL") for problem in problems)
