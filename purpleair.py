"""
purpleair.py

the PurpleAir module for porter, the Prometheus exporter

see https://api.purpleair.com/

To get an API key, send email to contact@purpleair.com
"""

import aqi, json, requests, prometheus_client, time

from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('purpleair_processing_seconds',
                                         'time of purpleair requests')

config = None # set by caller

@REQUEST_TIME.time()
def collect(target):
    resp = requests.get("https://www.purpleair.com/json?show={}".format(target))
    resp.raise_for_status()

    gauges = []
    def makegauge(metric, desc, labels=None):
        if labels is None:
            labels = ['sensor_id', 'sensor_name']
        gmf = GaugeMetricFamily(metric, desc, labels=labels)
        gauges.append(gmf)
        return gmf

    iaqi_gauge = makegauge('pm_25_iaqi', 'iAQI')
    aqandu_gauge = makegauge('pm_25_iaqi_AQandU', 'iAQI w/ AQandU correction')
    lrapa_gauge = makegauge('pm_25_iaqi_LRAPA', 'iAQI w/ LRAPA correction')
    ##rawpm1_gauge = makegauge('pm_10_10m_raw', 'raw PM1.0 (10 min average)')
    ##rawpm10_gauge = makegauge('pm_100_10m_raw', 'raw PM10 (10 min average)')
    tempc_gauge = makegauge('temp_c', 'temperature (degrees Celsius)')
    pressure_gauge = makegauge('sea_level_pressure_mb', 'sea level atmospheric pressure (millibars)')
    humidity_gauge = makegauge('humidity_pct', 'relative humidity (percent)')

    js = resp.json()
    for sensor in js.get("results", []): # may raise ValueError
        sensor_id = str(sensor.get("ID"))
        name = sensor.get("Label", '')
        stats = sensor.get("Stats")
        if stats:
            # we get the 10 minutely average because we may only obtain a value on a
            # 10 minutely basis, +/-
            pm25_raw = json.loads(stats).get("v1")
            if pm25_raw:
                pm25 = max(float(pm25_raw), 0)
                val = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25), algo=aqi.ALGO_EPA)
                iaqi_gauge.add_metric([sensor_id, name], val)

                # https://www.aqandu.org/airu_sensor#calibrationSection
                pm25_AQandU = 0.778 * pm25 + 2.65
                val = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_AQandU), algo=aqi.ALGO_EPA)
                aqandu_gauge.add_metric([sensor_id, name], val)

                # https://www.lrapa.org/DocumentCenter/View/4147/PurpleAir-Correction-Summary
                pm25_LRAPA = max(0.5 * float(pm25) - 0.66, 0)
                val = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_LRAPA), algo=aqi.ALGO_EPA)
                lrapa_gauge.add_metric([sensor_id, name], val)

        temp_f = sensor.get("temp_f")
        if temp_f:
            # 8 degree adjustment factor according to PurpleAir
            tempc_gauge.add_metric([sensor_id, name], round((float(temp_f)-32-8)*5/9, 1))
        pressure = sensor.get("pressure")
        if pressure:
            pressure_gauge.add_metric([sensor_id, name], float(pressure))
        humidity = sensor.get("humidity")
        if humidity:
            humidity_gauge.add_metric([sensor_id, name], float(humidity))
        
    return gauges
