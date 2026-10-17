"""Health-data service provider API.

Implements the health-data service spec endpoints using the typed
models from health_data_service.
"""

from __future__ import annotations

from datetime import datetime, timezone

import attrs
from health_data_service import (
    BreathRateAvg,
    Calories,
    Count,
    Distance,
    Duration,
    Efficiency,
    HeartRate,
    HeartRateAvg,
    HeartRateMax,
    HeartRateMin,
    HRV_RMSSD,
    HRVAvg,
    IntervalSample,
    MetricKind,
    MetricType,
    Pace,
    Sample,
    Score,
    SleepSession,
    SleepStage,
    SleepStages,
    Speed,
    TimeSeries,
)
from litestar import Request, get
from litestar.response import Response

from . import db, dedupe

SOURCE = "oura"


def _dt(ms: int) -> datetime:
    """Convert a stored unix-millisecond timestamp to a UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp (or bare date) into unix milliseconds."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# Maps DB metric names to display metadata for the time-series endpoint
METRIC_INFO = {
    "heart_rate": ("Heart Rate", "bpm"),
    "hrv": ("Heart Rate Variability (RMSSD)", "ms"),
    "sleep_stage": ("Sleep Stages", None),
    "readiness_score": ("Readiness Score", None),
    "readiness_activity_balance": ("Activity Balance", None),
    "readiness_body_temperature": ("Body Temperature", None),
    "readiness_hrv_balance": ("HRV Balance", None),
    "readiness_previous_day_activity": ("Previous Day Activity", None),
    "readiness_previous_night": ("Previous Night", None),
    "readiness_recovery_index": ("Recovery Index", None),
    "readiness_resting_heart_rate": ("Resting Heart Rate", None),
    "readiness_sleep_balance": ("Sleep Balance", None),
    "readiness_sleep_regularity": ("Sleep Regularity", None),
    "temperature_deviation": ("Temperature Deviation", "°C"),
    "temperature_trend_deviation": ("Temperature Trend Deviation", "°C"),
    "sleep_score": ("Sleep Score", None),
    "sleep_score_deep_sleep": ("Deep Sleep Score", None),
    "sleep_score_efficiency": ("Efficiency Score", None),
    "sleep_score_latency": ("Latency Score", None),
    "sleep_score_rem_sleep": ("REM Sleep Score", None),
    "sleep_score_restfulness": ("Restfulness Score", None),
    "sleep_score_timing": ("Timing Score", None),
    "sleep_score_total_sleep": ("Total Sleep Score", None),
}

DURATION_METRICS = {
    "total_sleep_duration", "deep_sleep_duration", "light_sleep_duration",
    "rem_sleep_duration", "awake_time", "time_in_bed", "latency",
}

SLEEP_SCALAR_FIELDS = {
    "total_sleep_duration": "total_duration",
    "deep_sleep_duration": "deep_sleep_duration",
    "light_sleep_duration": "light_sleep_duration",
    "rem_sleep_duration": "rem_sleep_duration",
    "awake_time": "awake_time",
    "time_in_bed": "time_in_bed",
    "latency": "latency",
    "average_heart_rate": "average_heart_rate",
    "lowest_heart_rate": "lowest_heart_rate",
    "average_hrv": "average_hrv",
    "average_breath": "average_breath",
    "efficiency": "efficiency",
    "restless_periods": "restless_periods",
}

STAGE_MAP = {1.0: SleepStage.DEEP, 2.0: SleepStage.LIGHT, 3.0: SleepStage.REM, 4.0: SleepStage.AWAKE}


def _serialize(obj):
    """Recursively convert attrs instances to dicts for JSON response."""
    if attrs.has(type(obj)):
        d = {}
        for field in attrs.fields(type(obj)):
            val = getattr(obj, field.name)
            d[field.name] = _serialize(val)
        return d
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _parse_ts(s: str) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _make_scalar(m_name: str, m_val: float):
    """Build the appropriate scalar metric type for a sleep session field."""
    if m_name in DURATION_METRICS:
        display = m_name.replace("_", " ").title()
        return Duration(value=m_val / 60.0, source=SOURCE, display_name=display)
    if m_name == "efficiency":
        return Efficiency(value=m_val, source=SOURCE)
    if m_name == "restless_periods":
        return Count(value=int(m_val), source=SOURCE, display_name="Restless Periods")
    if m_name == "average_heart_rate":
        return HeartRateAvg(value=m_val, source=SOURCE)
    if m_name == "lowest_heart_rate":
        return HeartRateMin(value=m_val, source=SOURCE)
    if m_name == "average_hrv":
        return HRVAvg(value=m_val, source=SOURCE)
    if m_name == "average_breath":
        return BreathRateAvg(value=m_val, source=SOURCE)
    return None


# ---------------------------------------------------------------------------
# /v1/metrics
# ---------------------------------------------------------------------------

@get("/api/v1/metrics")
async def service_list_metrics() -> dict:
    metrics: list[MetricType] = []
    async with db.connect() as conn:
        sample_metrics = await (await conn.execute(
            "SELECT DISTINCT metric FROM samples ORDER BY metric"
        )).fetchall()
        for r in sample_metrics:
            name = r[0]
            display, unit = METRIC_INFO.get(name, (name, None))
            metrics.append(MetricType(
                metric_id=name, display_name=display,
                kind=MetricKind.TIME_SERIES, unit=unit,
            ))

        daily = await (await conn.execute(
            "SELECT DISTINCT metric FROM daily_metrics ORDER BY metric"
        )).fetchall()
        for r in daily:
            name = r[0]
            if not any(m.metric_id == name for m in metrics):
                display, unit = METRIC_INFO.get(name, (name, None))
                metrics.append(MetricType(
                    metric_id=name, display_name=display,
                    kind=MetricKind.TIME_SERIES, unit=unit,
                ))

    return {"metrics": [_serialize(m) for m in metrics]}


# ---------------------------------------------------------------------------
# /v1/time-series
# ---------------------------------------------------------------------------

@get("/api/v1/time-series")
async def service_time_series(request: Request) -> dict:
    metric = request.query_params.get("metric")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = request.query_params.get("limit")

    if not metric:
        return {"error": "metric parameter required"}

    display, unit = METRIC_INFO.get(metric, (metric, None))

    is_daily = metric.startswith("readiness_") or metric.startswith("sleep_score") or metric.startswith("temperature_")

    if is_daily:
        ts = await _daily_time_series(metric, display, unit, start, end, limit)
    else:
        ts = await _sample_time_series(metric, display, unit, start, end, limit)

    return _serialize(ts)


async def _sample_time_series(metric, display, unit, start, end, limit) -> TimeSeries:
    conditions = ["metric = ?"]
    params: list = [metric]
    if start:
        conditions.append("timestamp_unix >= ?")
        params.append(_to_ms(start))
    if end:
        conditions.append("timestamp_unix <= ?")
        params.append(_to_ms(end))
    if limit:
        params.append(int(limit))

    where = " AND ".join(conditions)
    limit_clause = " LIMIT ?" if limit else ""

    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"SELECT timestamp_unix, end_unix, value FROM samples WHERE {where} ORDER BY timestamp_unix{limit_clause}",
            params,
        )).fetchall()

    samples: list[Sample | IntervalSample] = []
    for r in rows:
        ts = _dt(r[0])
        if r[1] is not None:
            samples.append(IntervalSample(timestamp=ts, value=r[2], end_timestamp=_dt(r[1])))
        else:
            samples.append(Sample(timestamp=ts, value=r[2]))

    return TimeSeries(
        metric_id=metric, display_name=display,
        unit=unit, source=SOURCE, samples=samples,
    )


async def _daily_time_series(metric, display, unit, start, end, limit) -> TimeSeries:
    conditions = ["metric = ?"]
    params: list = [metric]
    if start:
        conditions.append("date >= ?")
        params.append(start[:10] if len(start) > 10 else start)
    if end:
        conditions.append("date <= ?")
        params.append(end[:10] if len(end) > 10 else end)
    if limit:
        params.append(int(limit))

    where = " AND ".join(conditions)
    limit_clause = " LIMIT ?" if limit else ""

    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"SELECT date, value FROM daily_metrics WHERE {where} ORDER BY date{limit_clause}",
            params,
        )).fetchall()

    samples = [Sample(timestamp=_parse_ts(f"{r[0]}T00:00:00+00:00"), value=r[1]) for r in rows]
    return TimeSeries(
        metric_id=metric, display_name=display,
        unit=unit, source=SOURCE, samples=samples,
    )


# ---------------------------------------------------------------------------
# /v1/sleep-sessions
# ---------------------------------------------------------------------------

@get("/api/v1/sleep-sessions")
async def service_sleep_sessions(request: Request) -> dict:
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))
    include_series = request.query_params.get("include_series", "true").lower() != "false"

    conditions = []
    params: list = []
    if start:
        conditions.append("s.start_ts >= ?")
        params.append(start)
    if end:
        conditions.append("s.end_ts <= ?")
        params.append(end)
    params.append(limit)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    async with db.connect() as conn:
        sessions = await (await conn.execute(
            f"""SELECT s.id, s.source, s.source_id, s.start_ts, s.end_ts
                FROM sleep_sessions s {where}
                ORDER BY s.start_ts DESC LIMIT ?""",
            params,
        )).fetchall()

        result = []
        for s in sessions:
            session_id = s[0]
            kwargs: dict = {
                "start": _parse_ts(s[3]),
                "end": _parse_ts(s[4]),
                "source": s[1],
                "id": str(session_id),
            }

            metrics = await (await conn.execute(
                "SELECT metric, value FROM sleep_session_metrics WHERE sleep_session_id = ?",
                (session_id,),
            )).fetchall()

            for m_name, m_val in metrics:
                field = SLEEP_SCALAR_FIELDS.get(m_name)
                if not field:
                    continue
                scalar = _make_scalar(m_name, m_val)
                if scalar is not None:
                    kwargs[field] = scalar

            if not include_series:
                # Sleep score from daily_metrics (scalars only, no per-sample series)
                date_str = s[3][:10]
                score_row = await (await conn.execute(
                    "SELECT value FROM daily_metrics WHERE date = ? AND metric = 'sleep_score'",
                    (date_str,),
                )).fetchone()
                if score_row:
                    kwargs["sleep_score"] = Score(value=score_row[0], source=SOURCE, display_name="Sleep Score")
                result.append(SleepSession(**kwargs))
                continue

            # Sleep stages
            stage_rows = await (await conn.execute(
                """SELECT timestamp_unix, end_unix, value FROM samples
                   WHERE sleep_session_id = ? AND metric = 'sleep_stage'
                   ORDER BY timestamp_unix""",
                (session_id,),
            )).fetchall()

            if stage_rows:
                kwargs["stages"] = SleepStages(
                    source=SOURCE,
                    samples=[
                        IntervalSample(
                            timestamp=_dt(r[0]),
                            value=STAGE_MAP.get(r[2], SleepStage.UNKNOWN),
                            end_timestamp=_dt(r[1]),
                        )
                        for r in stage_rows
                    ],
                )

            # HR time series
            hr_rows = await (await conn.execute(
                """SELECT timestamp_unix, end_unix, value FROM samples
                   WHERE sleep_session_id = ? AND metric = 'heart_rate'
                   ORDER BY timestamp_unix""",
                (session_id,),
            )).fetchall()

            if hr_rows:
                hr_samples: list = []
                for r in hr_rows:
                    if r[1] is not None:
                        hr_samples.append(IntervalSample(timestamp=_dt(r[0]), value=r[2], end_timestamp=_dt(r[1])))
                    else:
                        hr_samples.append(Sample(timestamp=_dt(r[0]), value=r[2]))
                kwargs["heart_rate"] = HeartRate(source=SOURCE, samples=hr_samples)

            # HRV time series
            hrv_rows = await (await conn.execute(
                """SELECT timestamp_unix, end_unix, value FROM samples
                   WHERE sleep_session_id = ? AND metric = 'hrv'
                   ORDER BY timestamp_unix""",
                (session_id,),
            )).fetchall()

            if hrv_rows:
                hrv_samples: list = []
                for r in hrv_rows:
                    if r[1] is not None:
                        hrv_samples.append(IntervalSample(timestamp=_dt(r[0]), value=r[2], end_timestamp=_dt(r[1])))
                    else:
                        hrv_samples.append(Sample(timestamp=_dt(r[0]), value=r[2]))
                kwargs["hrv"] = HRV_RMSSD(source=SOURCE, samples=hrv_samples)

            # Sleep score from daily_metrics
            date_str = s[3][:10]
            score_row = await (await conn.execute(
                "SELECT value FROM daily_metrics WHERE date = ? AND metric = 'sleep_score'",
                (date_str,),
            )).fetchone()
            if score_row:
                kwargs["sleep_score"] = Score(value=score_row[0], source=SOURCE, display_name="Sleep Score")

            result.append(SleepSession(**kwargs))

    return {"data": [_serialize(s) for s in result]}


# ---------------------------------------------------------------------------
# /v1/workouts
# ---------------------------------------------------------------------------

# Types whose workouts carry distance/speed (spec: DistanceWorkout / SwimmingWorkout).
DISTANCE_TYPES = {"running", "walking", "hiking", "cycling", "snowboarding", "downhill_skiing", "swimming"}
# Foot-based types that also report pace.
PACE_TYPES = {"running", "walking", "hiking"}

_WORKOUT_COLUMNS = "source_id, workout_type, start_unix, end_unix, calories, distance"

# Instantaneous HR from the heartrate collection; excludes the 5-minute
# averages stored per sleep session so nothing is double-counted.
_WORKOUT_HR_WHERE = (
    "metric = 'heart_rate' AND sleep_session_id IS NULL"
    " AND timestamp_unix >= ? AND timestamp_unix <= ?"
)


def _build_workout(r, hr_stats, hr_samples=None) -> dict:
    """Spec workout dict from a workouts row and its (avg, max, min) HR over
    the workout window. hr_samples adds the full trace (detail endpoint only)."""
    source_id, wtype, start_ms, end_ms, calories, distance = r
    duration_s = (end_ms - start_ms) / 1000.0
    w: dict = {
        "start": _dt(start_ms).isoformat(),
        "end": _dt(end_ms).isoformat(),
        "id": source_id,
        "workout_type": wtype,
        "source": SOURCE,
        "duration": Duration(value=duration_s / 60.0, source=SOURCE),
    }
    if calories is not None:
        w["calories"] = Calories(value=calories, source=SOURCE)

    avg_hr, max_hr, min_hr = hr_stats or (None, None, None)
    if avg_hr is not None:
        w["average_heart_rate"] = HeartRateAvg(value=avg_hr, source=SOURCE)
        w["max_heart_rate"] = HeartRateMax(value=max_hr, source=SOURCE)
        w["lowest_heart_rate"] = HeartRateMin(value=min_hr, source=SOURCE)
    if hr_samples:
        w["heart_rate"] = HeartRate(source=SOURCE, samples=hr_samples)

    if wtype in DISTANCE_TYPES and distance:
        w["distance"] = Distance(value=distance, source=SOURCE)
        if duration_s > 0:
            w["average_speed"] = Speed(value=distance / duration_s, display_name="Avg Speed", source=SOURCE)
            if wtype in PACE_TYPES:
                w["average_pace"] = Pace(value=duration_s / (distance / 1000.0), display_name="Avg Pace", source=SOURCE)

    return _serialize(w)


async def _workout_hr_stats(conn, start_ms: int, end_ms: int):
    return await (await conn.execute(
        f"SELECT AVG(value), MAX(value), MIN(value) FROM samples WHERE {_WORKOUT_HR_WHERE}",
        (start_ms, end_ms),
    )).fetchone()


@get("/api/v1/workouts")
async def service_workouts(request: Request) -> dict:
    workout_type = request.query_params.get("workout_type")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

    conditions = []
    params: list = []
    if await dedupe.is_enabled():
        conditions.append("suppressed_by IS NULL")
    if workout_type:
        conditions.append("workout_type = ?")
        params.append(workout_type)
    if start:
        conditions.append("start_unix >= ?")
        params.append(_to_ms(start))
    if end:
        conditions.append("start_unix <= ?")
        params.append(_to_ms(end))
    params.append(limit)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"SELECT {_WORKOUT_COLUMNS} FROM workouts{where} ORDER BY start_unix DESC LIMIT ?",
            params,
        )).fetchall()
        # Summary only: the per-sample HR trace is served by the detail endpoint.
        workouts = [
            _build_workout(tuple(r), await _workout_hr_stats(conn, r[2], r[3]))
            for r in reversed(rows)
        ]
    return {"count": len(workouts), "data": workouts}


@get("/api/v1/workouts/{workout_id:str}")
async def service_workout(workout_id: str) -> Response:
    """One workout's full detail: the summary plus its heart-rate trace."""
    hide_suppressed = " AND suppressed_by IS NULL" if await dedupe.is_enabled() else ""
    async with db.connect() as conn:
        r = await (await conn.execute(
            f"SELECT {_WORKOUT_COLUMNS} FROM workouts WHERE source_id = ?{hide_suppressed}",
            (workout_id,),
        )).fetchone()
        if not r:
            return Response(content={"error": "not found"}, status_code=404)
        hr_rows = await (await conn.execute(
            f"SELECT timestamp_unix, value FROM samples WHERE {_WORKOUT_HR_WHERE} ORDER BY timestamp_unix",
            (r[2], r[3]),
        )).fetchall()
        hr_stats = await _workout_hr_stats(conn, r[2], r[3])

    hr_samples = [Sample(timestamp=_dt(t), value=v) for t, v in hr_rows]
    return Response(content=_build_workout(tuple(r), hr_stats, hr_samples))


# ---------------------------------------------------------------------------
# Route list for registration
# ---------------------------------------------------------------------------

service_routes = [
    service_list_metrics,
    service_time_series,
    service_sleep_sessions,
    service_workouts,
    service_workout,
]
