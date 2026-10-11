from fieldguide import weather

def test_best_day_is_the_pleasant_one(net):
    wk = weather.get_week(28.61, 77.21)
    assert len(wk["days"]) == 7
    assert wk["best_index"] == 2  # Monday: clear, 23°C, 5% rain, light wind
    assert wk["days"][2]["weekday_long"] == "Monday"
    assert wk["days"][2]["summary"] == "Clear"

def test_storm_and_heat_score_lower_than_a_calm_day(net):
    days = weather.get_week(28.61, 77.21)["days"]
    assert days[3]["score"] < days[0]["score"] < days[2]["score"]  # thunderstorm < hot & overcast < clear
    assert days[4]["score"] < days[2]["score"]                      # 36°C heatwave

def test_missing_data_is_never_picked_as_best(net):
    days = weather.get_week(28.61, 77.21)["days"]
    assert days[6]["score"] is None
    assert days[6]["summary"] == "Unknown"
    assert days[6]["sunset"] is None and days[6]["golden_hour"] is None

def test_golden_hour_is_one_hour_before_sunset(net):
    day = weather.get_week(28.61, 77.21)["days"][2]
    assert day["sunset"] == "17:48"
    assert day["golden_hour"] == "16:48"

def test_forecast_request_is_rounded_and_asks_for_local_timezone(net):
    weather.get_week(28.6139, 77.2090)
    url, params = net.calls[-1]
    assert params["latitude"] == 28.61 and params["longitude"] == 77.21
    assert params["timezone"] == "auto" and params["forecast_days"] == 7

def test_outdoor_score_edges():
    assert weather.outdoor_score(None, 10, 5, 0) is None
    assert weather.outdoor_score(20, None, 5, 0) is None
    assert weather.outdoor_score(20, 0, 5, 0) == 100
    assert weather.outdoor_score(-20, 100, 90, 99) == 0  # clamped, never negative