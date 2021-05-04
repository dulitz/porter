# ambientweather.py
#
# the Ambient Weather module for porter, the Prometheus exporter
#
# see https://ambientweather.docs.apiary.io/ and
# https://github.com/ambient-weather/api-docs/wiki/Device-Data-Specs


import requests, prometheus_client, time

from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('ambientweather_processing_seconds',
                                         'time of ambientweather requests')

@REQUEST_TIME.time()
def collect(config, target):
    awconfig = config.get('ambientweather')
    if not awconfig:
        print('no ambientweather configuration')
        return []

    metric_to_gauge = {}
    def makegauge(metric, desc, labels=None):
        already = metric_to_gauge.get(metric)
        if already:
            return already
        if labels is None:
            labels = ['macAddress', 'name']
        gmf = GaugeMetricFamily(metric, desc, labels=labels)
        metric_to_gauge[metric] = gmf
        return gmf

    for apikey in awconfig['apiKeys']:
        resp = requests.get("https://api.ambientweather.net/v1/devices?applicationKey={}&apiKey={}".format(awconfig['applicationKey'], apikey))
        resp.raise_for_status()

        for sensor in resp.json(): # may raise ValueError
            macaddress = sensor.get('macAddress')
            name = sensor.get('info', {}).get('name', '')
            labels = [macaddress, name]
            for (k, v) in sensor.get('lastData', {}).items():
                if k == 'winddir':
                    g = makegauge('ambientweather_wind_direction_degrees', 'wind direction')
                    g.add_metric(labels, float(v))
                elif k == 'windspeedmph':
                    g = makegauge('ambientweather_wind_speed_mph', 'wind speed')
                    g.add_metric(labels, float(v))
                elif k == 'windgustmph':
                    g = makegauge('ambientweather_wind_gust_10m_mph', 'max wind gust (10 min)')
                    g.add_metric(labels, float(v))
                elif k == 'humidity':
                    g = makegauge('ambientweather_outdoor_humidity_pct', '% relative humidity, outdoors')
                    g.add_metric(labels, float(v))
                elif k == 'humidityin':
                    g = makegauge('ambientweather_indoor_humidity_pct', '% relative humidity, indoors')
                    g.add_metric(labels, float(v))
                elif k.startswith('humidity'):
                    suffix = k[8:]
                    g = makegauge('ambientweather_humidity_%s_pct' % suffix, '%% relative humidity at sensor %s' % suffix)
                    g.add_metric(labels, float(v))
                elif k.startswith('batt'):
                    suffix = k[4:]
                    g = makegauge('ambientweather_battery_%s_good' % suffix, '1 if battery %s is good' % suffix)
                    g.add_metric(labels, int(v))
                elif k == 'tempf':
                    g = makegauge('ambientweather_outdoor_temp_c', 'outdoor temp (degrees C)')
                    g.add_metric(labels, round((float(v)-32)*5/9, 1))
                elif k == 'tempinf':
                    g = makegauge('ambientweather_indoor_temp_c', 'indoor temp (degrees C)')
                    g.add_metric(labels, round((float(v)-32)*5/9, 1))
                elif k.startswith('temp'):
                    suffix = k[4:]
                    if suffix.endswith('f'):
                        suffix = suffix[:len(suffix)-1]
                    g = makegauge('ambientweather_temp_%s_c' % suffix, 'temperature at sensor %s (degrees C)' % suffix)
                    g.add_metric(labels, round((float(v)-32)*5/9, 1))
                elif k == 'co2':
                    g = makegauge('ambientweather_indoor_co2_ppm', 'indoor CO2 concentration (ppm)')
                    g.add_metric(labels, float(v))
                elif k == 'pm25':
                    g = makegauge('ambientweather_pm25_ug_m3', 'PM2.5 in micrograms per cubic meter')
                    g.add_metric(labels, float(v))
                elif k == 'pm25_in':
                    g = makegauge('ambientweather_indoor_pm25_ug_m3', 'indoor PM2.5 in micrograms per cubic meter')
                    g.add_metric(labels, float(v))
                elif k == 'hourlyrainin':
                    g = makegauge('ambientweather_rainrate_in_hr', 'rate of rain in inches per hour')
                    g.add_metric(labels, float(v))
                elif k == '24hourrainin':
                    g = makegauge('ambientweather_rain_24hr_in', 'total rain last 24 hours (inches')
                    g.add_metric(labels, float(v))
                else:
                    pass
        
    return metric_to_gauge.values()
