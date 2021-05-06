# smartthings.py
#
# the SmartThings module for porter, the Prometheus exporter
#
# to get a Personal Access Token, visit https://account.smartthings.com/tokens
#
# see https://smartthings.developer.samsung.com/docs/api-ref/st-api.html
# and 

import requests, prometheus_client, time, threading

from prometheus_client.core import GaugeMetricFamily

# GET /locations
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#operation/listLocations
# GET /locations/{locationid}/rooms
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#operation/listRooms
# GET /rules?locationId={locationId}
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#tag/Rules
# GET /devices
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#operation/getDevices
# GET /devices/{deviceId}/status
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#operation/getDeviceStatus
# POST /devices/{deviceId}/commands
#     https://smartthings.developer.samsung.com/docs/api-ref/st-api.html#operation/executeDeviceCommands

REQUEST_TIME = prometheus_client.Summary('smartthings_processing_seconds',
                                         'time of smartthings requests')

class SmartThingsClient:
    API_PREFIX = 'https://api.smartthings.com/v1'
    CACHE_TIME = 60*30 # 30 minutes

    def __init__(self, config):
        self.config = config
        self.cache_cv = threading.Condition()
        self.cache = {}
        stconfig = config.get('smartthings')
        if not stconfig:
            raise Exception('no smartthings configuration')
        if not stconfig.get('accesstoken'):
            raise Exception('no smartthings accesstoken')
        if not stconfig.get('timeout'):
            stconfig['timeout'] = 10

    def bearer_json_request(self, command, path, data=None):
        endpoint = '%s%s' % (self.API_PREFIX, path)
        headers = { 'Authorization': 'Bearer %s' % self.config['smartthings']['accesstoken'] }
        timeout = self.config['smartthings']['timeout']
        if data: # depending on command, data may not be allowed as an argument
            resp = command(endpoint, headers=headers, timeout=timeout, data=data)
        else:
            resp = command(endpoint, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    def cache_request(self, rpath):
        now = time.time()
        with self.cache_cv:
            (t, val) = self.cache.get(rpath, (0, None))
            if now - t > self.CACHE_TIME:
                val = self.bearer_json_request(requests.get, rpath)
                self.cache[rpath] = (now, val)
        return val

    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

        stconfig = self.config['smartthings']

        metric_to_gauge = {}
        def makegauge(metric, desc, labels=None):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            if labels is None:
                labels = ['deviceId', 'nameLabel', 'location', 'health']
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        locations = {}
        for loc in self.cache_request('/locations').get('items', []):
            lid = loc.get('locationId')
            name = loc.get('name')
            if lid and name:
                locations[lid] = name

        resp = self.bearer_json_request(requests.get, '/devices')
        for device in resp.get('items', []):
            deviceid = device['deviceId']
            health = self.cache_request('/devices/%s/health' % deviceid)
            healthstate = health.get('state', '').lower() # offline, online
            lid = device['locationId']
            labelvalues = [deviceid, device['label'], locations.get(lid, lid), healthstate]

            status = self.bearer_json_request(requests.get, '/devices/%s/status' % deviceid)
            main = status.get('components', {}).get('main', {})
            seen = {}
            for (k, v) in main.items():
                if k == 'healthCheck':
                    continue # this data is totally useless (unrelated to health)
                if not v:
                    continue
                for (innerk, innerv) in v.items():
                    if (not innerv) or not innerv.get('value'):
                        continue
                    if innerk in seen:
                        continue
                    seen[innerk] = 1
                    unit = innerv.get('unit', '').lower()
                    value = innerv['value']
                    if unit == 'f': # convert to Celsius
                        unit = 'c'
                        value = round((float(value)-32)*5/9, 1)
                    if innerk == 'switch':
                        g = makegauge('switch_on', '1 if switch on, 0 if switch off, -1 otherwise')
                        g.add_metric(labelvalues, 1 if value == 'on' else 0 if value == 'off' else -1)
                    elif innerk == 'water':
                        g = makegauge('water_dry', '1 if dry, 0 if wet, -1 otherwise')
                        g.add_metric(labelvalues, 1 if value == 'dry' else 0 if value == 'wet' else -1)
                    elif innerk == 'door' or innerk == 'contact':
                        g = makegauge('%s_closed' % innerk, '1 if closed, 0 if open, -1 otherwise')
                        g.add_metric(labelvalues, 1 if value == 'closed' else 0 if value == 'open' else -1)
                    elif innerk == 'lock':
                        g = makegauge('lock_locked', '1 if locked, 0 if unlocked, -1 otherwise')
                        g.add_metric(labelvalues, 1 if value == 'locked' else 0 if value == 'unlocked' else -1)
                    elif unit == '%':
                        # innerk: battery, level
                        g = makegauge('%s_pct' % innerk, 'percentage of full %s' % innerk)
                        g.add_metric(labelvalues, float(value))
                    elif unit == 'w' or unit == 'kwh' or unit == 'f' or unit == 'c':
                        # innerk: power, energy, temperature
                        g = makegauge('%s_%s' % (innerk, unit), '%s (%s)' % (innerk, unit))
                        g.add_metric(labelvalues, float(value))
                    elif innerk == 'color' or innerk == 'hue' or innerk == 'saturation':
                        g = makegauge('%s' % innerk, '%s of light' % innerk)
                        g.add_metric(labelvalues, float(value))
                    # ignoring innerk: threeAxis, acceleration, and things for locks
                    # for locks, innerv['data'] is dict with 'method' key -- defines label

        return metric_to_gauge.values()

if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = SmartThingsClient(config)

    res = client.bearer_json_request(requests.get, '/devices')
    for device in res.get('items', []):
        print('%s %s (%s)' % (device['deviceId'], device['label'], device['name']))
        print('    @ %s' % (device['locationId']))
        for component in device['components']:
            cid = component['id']
            if cid != 'main':
                print('    --- component %s %s' % (cid, component.get('label', '<none>')))
            print('    ',end='')
            for capability in component['capabilities']:
                print('%s/%d ' % (capability['id'], capability['version']),end='')
            print('')
        health = client.bearer_json_request(requests.get, '/devices/%s/health' % device['deviceId'])
        healthstate = health.get('state', '').lower() # offline, online
        print('    health:', healthstate)
        status = client.bearer_json_request(requests.get, '/devices/%s/status' % device['deviceId'])
        main = status.get('components', {}).get('main', {})
        for (k, v) in main.items():
            if k == 'healthCheck':
                continue # this data is totally useless (unrelated to health)
            if not v:
                continue
            for (innerk, innerv) in v.items():
                # e.g. innerk is "water" and innerv is { 'value': 'dry', 'timestamp': '...' }
                if innerk == 'temperature' and innerv.get('unit', '') == 'F':
                    # convert innerv['value'] to Celsius
                    pass
                print(innerk, innerv) # these can be duplicated and innerv can be None
                # innerk == switch, innerv['value'] == 'on' or 'off'
                # innerk == water, innerv['value'] == 'dry' or 'wet'
                # innerk == door, innerv['value'] == 'closed' or 'open'
                # innerk == power, innerv['unit'] == 'W'
                # innerk == battery, innerv['unit'] == '%'
                # innerk == level, innerv['unit'] == '%'
                # innerv['data'] is dict with 'method' key -- defines label
