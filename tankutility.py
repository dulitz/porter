# tankutility.py
#
# the Tank Utility module for porter, the Prometheus exporter
#
# see http://apidocs.tankutility.com/#introduction

import requests, prometheus_client, time, threading

from requests.auth import HTTPBasicAuth
from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('tankutility_processing_seconds',
                                         'time of tankutility requests')

  
class TankUtilityClient:
    API_PREFIX = 'https://data.tankutility.com/api'
    MAX_TOKEN_AGE = 12 * 60 * 60

    def __init__(self, config):
        self.config = config
        self.cv = threading.Condition()
        self.target_to_tokentime = {}
        tuconfig = config.get('tankutility')
        if not tuconfig:
            raise Exception('no tankutility configuration')
        if not tuconfig.get('credentials'):
            raise Exception('no tankutility credentials')
        if not tuconfig.get('timeout'):
            tuconfig['timeout'] = 10
    
    def _get_token(self, target):
        """Only gets the token if it is halfway to its 24 hour expiration."""
        with self.cv:
            (token, tokentime) = self.target_to_tokentime.get(target, (None, 0))
            if time.time() - tokentime < self.MAX_TOKEN_AGE:
                return token
            password = self.config['tankutility']['credentials'].get(target)
            if not password:
                raise Exception(f'no tankutility credentials for {target}')
            timeout = self.config['tankutility']['timeout']
            resp = requests.get(f'{self.API_PREFIX}/getToken', timeout=timeout,
                                auth=HTTPBasicAuth(target, password))
            resp.raise_for_status()
            if resp.status_code != 200:
                # other 'success' statuses are not really success
                raise Exception(f'unexpected status {resp.status_code}')
            accesstoken = resp.json()['token']
            self.target_to_tokentime[target] = (accesstoken, time.time())
            return accesstoken

    def bearer_json_request(self, target, command, path, data=None):
        accesstoken = self._get_token(target)
        endpoint = f'{self.API_PREFIX}{path}?token={accesstoken}'
        timeout = self.config['tankutility']['timeout']
        if data: # depending on command, data may not be allowed as an argument
            resp = command(endpoint, timeout=timeout, data=data)
        else:
            resp = command(endpoint, timeout=timeout)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()
  
    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

        metric_to_gauge = {}
        def makegauge(metric, desc, labels=None):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            if labels is None:
                labels = ['deviceId', 'nameLabel']
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        resp = self.bearer_json_request(target, requests.get, '/devices')
        for device in resp.get('devices', []):
            d = self.bearer_json_request(target, requests.get, f'/devices/{device}').get('device', {})
            labelvalues = [d.get('short_device_id', device), d['name']]
            capacity = d.get('capacity')
            if capacity:
                g = makegauge('tank_capacity_gal', 'volumetric capacity of the tank (gal)')
                g.add_metric(labelvalues, capacity)
            pct = d.get('lastReading', {}).get('tank')
            if pct is not None:
                g = makegauge('tank_level_pct', 'tank percent of full')
                g.add_metric(labelvalues, pct)
            temp_f = d.get('lastReading', {}).get('temperature')
            if temp_f is not None:
                g = makegauge('temp_c', 'ambient temperature of the tank (degrees Celsius)')
                g.add_metric(labelvalues, round((float(temp_f)-32)*5/9, 1))
            lasttime = d.get('lastReading', {}).get('time')
            if lasttime:
                g = makegauge('last_reading', 'time of last reading (ticks since epoch)')
                g.add_metric(labelvalues, lasttime)
            firmware = d.get('lastReading', {}).get('sw_rev')
            if firmware:
                g = makegauge('firmware_rev', 'firmware revision')
                g.add_metric(labelvalues, firmware)
            infofields = ['device_id', 'short_device_id', 'name', 'address', 'account_id',
                          'fuel_type', 'status', 'orientation', 'consumption_types']
            info = [(f, d[f]) for f in infofields if d.get(f)]
            if info:
                labels = [p[0] for p in info]
                g = makegauge('tank_info', 'labelled information about the tank', labels)
                g.add_metric([p[1] for p in info], 1)
            fueltype = d.get('fuel_type')
            if fueltype:
                g = makegauge('fuel_type', '0 if propane, -1 if other')
                g.add_metric(labelvalues, 0 if fueltype == 'propane' else -1)
            status = d.get('status')
            if status:
                g = makegauge('tank_status', '1 if deployed, 0 if not')
                g.add_metric(labelvalues, 1 if status == 'deployed' else 0)
            orientation = d.get('orientation')
            if orientation:
                g = makegauge('tank_orientation', '1 if horizontal, 0 if vertical, -1 otherwise')
                g.add_metric(labelvalues, 1 if orientation == 'horizontal' else 0 if orientation == 'vertical' else -1)
            batterywarn = d.get('battery_warn')
            batterycrit = d.get('battery_crit')
            if batterywarn or batterycrit:
                g = makegauge('low_battery', '2 if battery critical, 1 if battery warning, 0 otherwise')
                g.add_metric(labelvalues, 2 if batterycrit else 1 if batterywarn else 0)
            txinterval = d.get('transmission_interval')
            if txinterval:
                g = makegauge('report_interval', 'expected time between reports (sec)')
                g.add_metric(labelvalues, txinterval)
                
        return metric_to_gauge.values()


if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = TankUtilityClient(config)
    target = sys.argv[2]

    resp = client.bearer_json_request(target, requests.get, '/devices')
    for device in resp.get('devices', []):
        d = client.bearer_json_request(target, requests.get, f'/devices/{device}').get('device', {})
        print(d)
