from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.weather_client import WeatherClient


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


def payload(desc, temp="22", wind="8", precip="0"):
    return {
        "current_condition": [
            {
                "weatherDesc": [{"value": desc}],
                "temp_C": temp,
                "windspeedKmph": wind,
                "precipMM": precip,
                "humidity": "50",
            }
        ]
    }


def hourly_payload():
    return {
        "current_condition": [
            {
                "weatherDesc": [{"value": "Clear"}],
                "temp_C": "24",
                "windspeedKmph": "6",
                "precipMM": "0",
                "humidity": "45",
            }
        ],
        "weather": [
            {
                "date": "2026-06-03",
                "maxtempC": "27",
                "mintempC": "18",
                "hourly": [
                    {
                        "time": "1500",
                        "weatherDesc": [{"value": "Light rain"}],
                        "tempC": "19",
                        "windspeedKmph": "10",
                        "precipMM": "2",
                        "humidity": "82",
                    }
                ],
            }
        ],
    }


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def build(desc, temp="22", wind="8", precip="0"):
    client = WeatherClient(session=FakeSession(payload(desc, temp, wind, precip)))
    return client.build_weather_context("北京", time_context={"current_datetime": "2026-06-03T20:00:00+08:00"})


def test_rain():
    ctx = build("Light rain", precip="2")
    assert_true(ctx["condition"] == "rain", "rain should be detected")
    assert_true(ctx["indoor_preferred"] is True, "rain should prefer indoor")


def test_hot():
    ctx = build("Sunny", temp="35")
    assert_true(ctx["condition"] == "hot", "hot weather should be detected")
    assert_true("high_temperature" in ctx["warnings"], "hot warning should be present")


def test_wind():
    ctx = build("Clear", wind="40")
    assert_true(ctx["wind_risk"] == "high", "strong wind should be high risk")
    assert_true(ctx["outdoor_suitability"] == "low", "strong wind should lower outdoor suitability")


def test_failure_fallback():
    client = WeatherClient(session=FakeSession(error=RuntimeError("network down")))
    ctx = client.build_weather_context("北京", time_context={})
    assert_true(ctx["source"] == "unavailable", "failure should use unavailable fallback")
    assert_true(ctx["warnings"], "fallback should include warning")


def test_target_window_uses_hourly_forecast():
    client = WeatherClient(session=FakeSession(hourly_payload()))
    ctx = client.build_weather_context(
        "北京",
        time_context={
            "current_datetime": "2026-06-03T09:00:00+08:00",
            "inferred_start_time": "2026-06-03T15:00:00+08:00",
        },
    )
    assert_true(ctx["forecast_basis"] == "forecast_hourly", "target start time should select hourly forecast")
    assert_true(ctx["condition"] == "rain", "hourly forecast rain should drive weather condition")
    assert_true(ctx["temperature_c"] == 19.0, "hourly temp should be used")


def test_traveler_summary_degree_symbol_variants():
    client = WeatherClient()
    legacy_degree = "°".encode("utf-8").decode("gbk", errors="replace")
    normal = client._parse_traveler_summary("北京", "北京当前天气：Mist，气温 22°C，湿度 80%。", "", "")
    mojibake = client._parse_traveler_summary("北京", f"北京当前天气：Mist，气温 22{legacy_degree}C，湿度 80%。", "", "")
    assert_true(normal["temperature_c"] == 22.0, "normal degree symbol should parse")
    assert_true(mojibake["temperature_c"] == 22.0, "mojibake degree symbol should parse")
    assert_true(normal["condition"] == "cloudy", "Mist should normalize to cloudy")


def test_weather_client_is_direct_structured_service():
    client = WeatherClient(session=FakeSession(payload("Clear", temp="23")))
    ctx = client.build_weather_context("北京", time_context={})
    assert_true(ctx["source"] == "wttr.in", "WeatherClient should expose direct structured weather source")
    assert_true("condition" in ctx, "structured condition should be present")


def run_all_tests():
    for test in (
        test_rain,
        test_hot,
        test_wind,
        test_failure_fallback,
        test_target_window_uses_hourly_forecast,
        test_traveler_summary_degree_symbol_variants,
        test_weather_client_is_direct_structured_service,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    run_all_tests()
