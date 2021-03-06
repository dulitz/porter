# ambientweather.py
#
# the Ambient Weather module for porter, the Prometheus exporter
#
# see https://ambientweather.docs.apiary.io/ and
# https://github.com/ambient-weather/api-docs/wiki/Device-Data-Specs


import logging, requests, prometheus_client, time
from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('ambientweather_processing_seconds',
                                         'time of ambientweather requests')
LOGGER = logging.getLogger('porter.ambientweather')

config = None # set by caller

@REQUEST_TIME.time()
def collect(target):
    awconfig = config.get('ambientweather')
    if not awconfig:
        print('no ambientweather configuration')
        return []

    metric_to_gauge = {}
    def makegauge(metric, desc, labels=[]):
        already = metric_to_gauge.get(metric)
        if already:
            return already
        alllabels = ['macAddress', 'name'] + labels
        gmf = GaugeMetricFamily(metric, desc, labels=alllabels)
        metric_to_gauge[metric] = gmf
        return gmf
    def addgaugesensormetric(metric, desc, labelvalues, sensor, value):
        gmf = makegauge(metric, desc, ['sensor'])
        gmf.add_metric(labelvalues + [sensor], value)

    for apikey in awconfig['apiKeys']:
        resp = requests.get("https://api.ambientweather.net/v1/devices?applicationKey={}&apiKey={}".format(awconfig['applicationKey'], apikey))
        if resp.status_code == 401:
            (beforequery, q, after) = resp.request.url.partition('?')
            LOGGER.info(f'status 401 for {beforequery} should be transient')
            continue
        resp.raise_for_status()

        for sensor in resp.json(): # may raise ValueError
            macaddress = sensor.get('macAddress')
            name = sensor.get('info', {}).get('name', '')
            labels = [macaddress, name]
            for (k, v) in sensor.get('lastData', {}).items():
                if v is None:
                    LOGGER.debug(f'{k} has value None for sensor {name}')
                    continue
                if k == 'winddir':
                    g = makegauge('wind_direction_degrees', 'wind direction')
                    g.add_metric(labels, float(v))
                elif k == 'windspeedmph':
                    g = makegauge('wind_speed_mph', 'wind speed')
                    g.add_metric(labels, float(v))
                elif k == 'windgustmph':
                    g = makegauge('wind_gust_10m_mph', 'max wind gust (10 min)')
                    g.add_metric(labels, float(v))
                elif k == 'humidity':
                    addgaugesensormetric('humidity_pct', '%% relative humidity',
                                         labels, 'outdoor', float(v))
                elif k == 'humidityin':
                    addgaugesensormetric('humidity_pct', '%% relative humidity',
                                         labels, 'indoor', float(v))
                elif k.startswith('humidity'):
                    suffix = k[8:]
                    addgaugesensormetric('humidity_pct', '%% relative humidity',
                                         labels, suffix, float(v))
                elif k.startswith('batt'):
                    suffix = k[4:]
                    addgaugesensormetric('battery_good', '1 if battery is good',
                                         labels, suffix, int(v))
                elif k == 'tempf':
                    addgaugesensormetric('temp_c', 'temperature (degrees Celsius)',
                                         labels, 'outdoor', round((float(v)-32)*5/9, 1))
                elif k == 'tempinf':
                    addgaugesensormetric('temp_c', 'temperature (degrees Celsius)',
                                         labels, 'indoor', round((float(v)-32)*5/9, 1))
                elif k.startswith('temp'):
                    suffix = k[4:]
                    if suffix.endswith('f'):
                        suffix = suffix[:len(suffix)-1]
                        celsius = round((float(v)-32)*5/9, 1)
                    else:
                        celsius = float(v)
                    addgaugesensormetric('temp_c', 'temperature (degrees Celsius)',
                                         labels, suffix, celsius)
                elif k == 'co2':
                    g = makegauge('indoor_co2_ppm', 'indoor CO2 concentration (ppm)')
                    g.add_metric(labels, float(v))
                elif k == 'pm25':
                    g = makegauge('pm25_ug_m3', 'PM2.5 in micrograms per cubic meter')
                    g.add_metric(labels, float(v))
                elif k == 'pm25_in':
                    g = makegauge('indoor_pm25_ug_m3', 'indoor PM2.5 in micrograms per cubic meter')
                    g.add_metric(labels, float(v))
                elif k == 'hourlyrainin':
                    g = makegauge('rainrate_in_hr', 'rate of rain in inches per hour')
                    g.add_metric(labels, float(v))
                elif k == '24hourrainin':
                    g = makegauge('rain_24hr_in', 'total rain last 24 hours (inches')
                    g.add_metric(labels, float(v))
                else:
                    pass
        
    return metric_to_gauge.values()
