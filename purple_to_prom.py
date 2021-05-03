
# from https://github.com/wbertelsen/purpleair-to-prometheus/blob/main/purple_to_prom.py

# last modified 12 December 2020, current as of 1 May 2021

# Original Author: https://github.com/wbertelsen/

# MIT License

import time
import traceback
from typing import List


import aqi
import json
import requests
import argparse
import prometheus_client


aqi_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi', 'iAQI (10 min average)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
aqi_AQandU_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi_AQandU', 'iAQI (10 min average) w/ AQandU correction',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
aqi_LRAPA_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi_LRAPA', 'iAQI (10 min average) w/ LRAPA correction',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
temp_g = prometheus_client.Gauge(
    'purpleair_temp_f', 'Sensor temp reading (degrees Fahrenheit)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
humidity_g = prometheus_client.Gauge(
    'purpleair_humidity_pct', 'Sensor humidity reading (percent)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
pressure_g = prometheus_client.Gauge(
    'purpleair_pressure_mb', 'Sensor pressure reading (millibars)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)

def clear_metrics():
    # NOTE: there's no official way to support it unless we convert this script
    # to a "custom collector".
    # See https://github.com/prometheus/client_python/issues/277
    for g in [aqi_g, aqi_AQandU_g, aqi_LRAPA_g, temp_g, pressure_g,
              humidity_g]:
        with g._lock():
            g._metrics.clear()

def check_sensor(parent_sensor_id: str) -> None:
    resp = requests.get("https://www.purpleair.com/json?show={}".format(parent_sensor_id))
    if resp.status_code != 200:
        clear_metrics()
        raise Exception("got {} responde code from purpleair".format(resp.status_code))

    try:
        resp_json = resp.json()
    except ValueError:
        clear_metrics()
        raise
    for sensor in resp_json.get("results"):
        sensor_id = sensor.get("ID")
        name = sensor.get("Label")
        stats = sensor.get("Stats")
        temp_f = sensor.get("temp_f")
        humidity = sensor.get("humidity")
        pressure = sensor.get("pressure")
        try:
            if stats:
                stats = json.loads(stats)
                pm25_10min_raw = stats.get("v1")
                if pm25_10min_raw:
                    pm25_10min = max(float(pm25_10min_raw), 0)
                    i_aqi = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min), algo=aqi.ALGO_EPA)
                    aqi_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(i_aqi)

                    # https://www.aqandu.org/airu_sensor#calibrationSection
                    pm25_10min_AQandU = 0.778 * float(pm25_10min) + 2.65
                    i_aqi_AQandU = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min_AQandU), algo=aqi.ALGO_EPA)
                    aqi_AQandU_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(i_aqi_AQandU)


                    # https://www.lrapa.org/DocumentCenter/View/4147/PurpleAir-Correction-Summary
                    pm25_10min_LRAPA = max(0.5 * float(pm25_10min) - 0.66, 0)
                    i_aqi_LRAPA = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min_LRAPA), algo=aqi.ALGO_EPA)
                    aqi_LRAPA_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(i_aqi_LRAPA)

                if temp_f:
                    temp_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(float(temp_f))
                if pressure:
                    pressure_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(float(pressure))
                if humidity:
                    humidity_g.labels(
                        parent_sensor_id=parent_sensor_id, sensor_id=sensor_id, sensor_name=name
                    ).set(float(humidity))
        except Exception:
            try:
                # Stop exporting metrics, instead of showing as a flat line.
                aqi_g.remove(parent_sensor_id, sensor_id, name)
                aqi_AQandU_g.remove(parent_sensor_id, sensor_id, name)
                aqi_LRAPA_g.remove(parent_sensor_id, sensor_id, name)
                temp_g.remove(parent_sensor_id, sensor_id, name)
                pressure_g.remove(parent_sensor_id, sensor_id, name)
                humidity_g.remove(parent_sensor_id, sensor_id, name)
            except KeyError:
                # No data produced yet. Silently ignore it.
                pass
            raise


def poll(sensor_ids: List[str], refresh_seconds: int) -> None:
    while True:
        print("refreshing sensors...", flush=True)
        for sensor_id in sensor_ids:
            try:
                check_sensor(sensor_id)
            except Exception:
                traceback.print_exc()
                print("Error fetching sensor data, sleeping till next poll")
                break
        time.sleep(refresh_seconds)


def main():
    parser = argparse.ArgumentParser(
        description="Gets sensor data from purple air, converts it to AQI, and exports it to prometheus"
    )
    parser.add_argument('--sensor-ids', nargs="+", help="Sensors to collect from", required=True)
    parser.add_argument("--port", type=int, help="What port to serve prometheus metrics on", default=9760)
    parser.add_argument("--refresh-seconds", type=int, help="How often to refresh", default=60)
    args = parser.parse_args()

    prometheus_client.start_http_server(args.port)

    print("Serving prometheus metrics on {}/metrics".format(args.port))
    poll(args.sensor_ids, args.refresh_seconds)
