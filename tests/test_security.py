import meal_relay


def test_meal_relay_requires_matching_api_key(monkeypatch):
    handler = object.__new__(meal_relay.MealHandler)
    handler.headers = {"X-API-Key": "good-key"}

    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "")
    assert handler._authorized() is False

    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    assert handler._authorized() is True

    handler.headers = {"X-API-Key": "wrong-key"}
    assert handler._authorized() is False
