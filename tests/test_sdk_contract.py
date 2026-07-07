"""Contract tests pinning the google-genai SDK surface this project depends on.

telegram_bot.py constructs genai.Client(api_key=...) and calls
client.models.generate_content(...), then reads response.text. If a future
blind upgrade of google-genai renames or removes any of these, these tests
fail fast in CI instead of at meal time.

All assertions run offline: constructing a Client or a config object performs
no network I/O.
"""

from google import genai
from google.genai import types


def test_genai_exposes_client():
    assert hasattr(genai, "Client")


def test_client_models_generate_content_exists():
    # A dummy API key is fine: Client construction does not hit the network.
    client = genai.Client(api_key="test-key")
    assert hasattr(client.models, "generate_content")
    assert callable(client.models.generate_content)


def test_generate_content_config_accepts_response_mime_type():
    config = types.GenerateContentConfig(response_mime_type="application/json")
    assert config.response_mime_type == "application/json"


def test_generate_content_response_has_text_attribute():
    assert hasattr(types.GenerateContentResponse, "text")
