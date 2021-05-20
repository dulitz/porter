# rachio.py
#
# the Rachio module for porter, the Prometheus exporter
#
# see https://rachio.readme.io/docs/authentication
# Authorization: Bearer

import requests, prometheus_client, time, threading

from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('rachio_processing_seconds',
                                         'time of rachio requests')

  
class RachioClient:
    API_PREFIX = 'https://api.rach.io/1'
    MAX_TOKEN_AGE = 12 * 60 * 60

    def __init__(self, config):
        self.config = config
        self.cache_cv = threading.Condition()
        self.target_cache = {}

        myconfig = config.get('rachio')
        if not myconfig:
            raise Exception('no rachio configuration')
        if not myconfig.get('credentials'):
            raise Exception('no rachio credentials')
        if not myconfig.get('timeout'):
            myconfig['timeout'] = 10
        if not myconfig.get('cachetime'):
            myconfig['cachetime'] = 2 * 3600

    def _increment(self, d, key, increment=1):
        newv = d.get(key, 0) + increment
        d[key] = newv
        return newv

    def _bearer_json_request(self, target, command, path, data=None):
        endpoint = f'{self.API_PREFIX}{path}'
        token = self.config['rachio']['credentials'].get(target)
        if token is None:
            raise Exception(f'no rachio credentials for target {target}')
        headers = { 'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json' }
        timeout = self.config['rachio']['timeout']
        if data: # depending on command, data may not be allowed as an argument
            resp = command(endpoint, headers=headers, timeout=timeout, data=data)
        else:
            resp = command(endpoint, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()
    
    def _cache_refresh(self, target):
        with self.cache_cv:
            (cacheid, cacheinfo, cachetime) = self.target_cache.get(target, (None, None, 0))
            if time.time() - cachetime > self.config['rachio']['cachetime']:
                resp = self._bearer_json_request(target, requests.get, '/public/person/info')
                cacheid = resp['id']
                cacheinfo = self._bearer_json_request(target, requests.get, f'/public/person/{cacheid}')
                self.target_cache[target] = (cacheid, cacheinfo, time.time())
            return cacheinfo

    def get_info_cache(self, target):
        return self._cache_refresh(target)

    def get_info(self, target):
        now = time.time()
        self._cache_refresh(target) # in case cache is empty
        with self.cache_cv:
            (cacheid, cacheinfo, cachetime) = self.target_cache[target] # refresh made sure this exists
            if cachetime < now:
                # then the cache did not refresh
                cache_info = self._bearer_json_request(target, requests.get, f'/public/person/{cache_id}')
                self.target_cache[target] = (cacheid, cacheinfo, cachetime)
                # we don't update cachetime because we didn't refetch cacheid
            return cacheinfo

    def get_deviceids(self, info):
        return [d['id'] for d in info.get('devices', {})]
    
    def get_events_for_device(self, target, deviceid, starting, ending=time.time()):
        return self._bearer_json_request(target, requests.get, f'/public/device/{deviceid}/event?startTime={int(starting*1000)}&endTime={int(ending*1000)}')

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

        for device in self.get_info(target).get('devices', []):
            devid = device['id']
            devname = device.get('name', '')
            labelvalues = [devid, devname]

            g = makegauge('up', '1 if device is on and communicating, 0 otherwise')
            g.add_metric(labelvalues, device.get('status', '').lower() == 'online')

            for z in device.get('zones', []):
                if z.get('enabled', False):
                    znum = z['zoneNumber']
                    zname = z.get('name', '')
                    morelabels = ['deviceId', 'nameLabel', 'zone', 'zonename']
                    labelvalues = [devid, devname, str(znum), zname]
                    last_duration = z.get('lastWateredDuration')
                    last_date = z.get('lastWateredDate')
                    if last_date:
                        g = makegauge('last_watered', 'when the zone was last watered (sec past epoch)', labels=morelabels)
                        g.add_metric(labelvalues, last_date/1000.0)
                    if last_duration:
                        g = makegauge('last_watered_duration_sec', 'duration of last watering (sec)', labels=morelabels)
                        g.add_metric(labelvalues, last_duration)
        
        return metric_to_gauge.values()


if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = RachioClient(config)
    target = sys.argv[2]

    i = client.get_info_cache(target)
    print(json.dumps(i, indent=2))

    for d in client.get_deviceids(client.get_info_cache(target)):
        starting = time.time() - 86400
        events = client.get_events_for_device(target, d, starting, time.time())
        print(json.dumps(events, indent=2))
