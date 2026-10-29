import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywall import config


def make_config(tmp_path, **overrides):
    values = {
        "app": "paywall_worker",
        "domain": "pay.example.com",
        "path": "/",
        "data_dir": str(tmp_path),
        "signing_secret": "s3cret-signing-key",
        "flask_secret": "flask-secret",
        "origin_secret": "origin-secret",
        "protected_routes": "blog.example.com/*\nnews.example.org/premium/*",
        "stripe_secret_key": "sk_test_123",
        "stripe_webhook_secret": "whsec_test",
        "enable_pass": "1",
        "enable_pwyw": "1",
    }
    values.update({k: str(v) for k, v in overrides.items()})
    return config.from_dict(values)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)
