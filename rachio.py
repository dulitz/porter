# rachio.py
#
# the Rachio module for porter, the Prometheus exporter
#
# see https://rachio.readme.io/docs/authentication
# Authorization: Bearer

import logging, prometheus_client, requests, time, threading

from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('rachio_processing_seconds',
                                         'time of rachio requests')

LOGGER = logging.getLogger('porter.rachio')

  
class RachioClient:
    API_PREFIX = 'https://api.rach.io/1'
    MAX_TOKEN_AGE = 12 * 60 * 60

    def __init__(self, config):
        self.clientstarttime = time.time()
        self.config = config
        self.cache_cv = threading.Condition()
        self.target_cache = {}
        self.zone_cache = {}
        self.lasteventtime = self.clientstarttime

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
                cache_info = self._bearer_json_request(target, requests.get, f'/public/person/{cacheid}')
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
            allzones = device.get('zones', [])

            for z in allzones:
                if z.get('enabled', False):
                    znum = z['zoneNumber']
                    zname = z.get('name', '')
                    morelabels = ['deviceId', 'nameLabel', 'zone', 'zonename']
                    labelvalues = [devid, devname, str(znum), zname]
                    last_duration = z.get('lastWateredDuration')
                    last_date = z.get('lastWateredDate')
                    with self.cache_cv:
                        self.zone_cache[zname] = znum
                    if last_date:
                        g = makegauge('last_watered', 'when the zone was last watered (sec past epoch)', labels=morelabels)
                        g.add_metric(labelvalues, last_date/1000.0)
                    if last_duration:
                        # note: this is unreliable and doesn't show for some zones
                        g = makegauge('last_watered_duration_sec', 'duration of last watering (sec)', labels=morelabels)
                        g.add_metric(labelvalues, last_duration)

            now = time.time()
            # get last hour's events to make sure we didn't miss any
            for z in self.get_events_for_device(target, devid, now - 3600, now):
                if z.get('subType', '') == 'ZONE_COMPLETED' and z.get('topic', '') == 'WATERING':
                    s = z.get('summary', '')
                    (zname, sep, last) = s.partition(' completed watering at ')
                    if not sep:
                        LOGGER.warning(f'could not parse zone summary {s}')
                        continue
                    parsed = last.rstrip().rstrip('.').split(' ')
                    mult = 60 if parsed[-1] == 'minutes' else 1 if parsed[-1] == 'seconds' else -1
                    try:
                        val = int(parsed[-2])
                    except ValueError:
                        val = -1
                    if mult == -1 or val == -1:
                        LOGGER.warning(f'could not parse zone summary {s} with parsed {parsed} and val {val}')
                        continue
                    seconds = mult * val
                    with self.cache_cv:
                        eventtime = z.get('eventDate', 0) / 1000
                        if eventtime <= self.lasteventtime:
                            continue
                        self.lasteventtime = eventtime
                        znum = self.zone_cache.get(zname, '')
                        if znum == '':
                            # It really shouldn't be because we just iterated
                            # through all the zone names above. There is a race
                            # condition though, if the user changes the zone name.
                            LOGGER.warning(f'zone {zname} not in zone cache; ignoring {s}')
                            continue
                        labels = ['deviceId', 'nameLabel', 'zone', 'zonename']
                        labelvalues = [devid, devname, znum, zname]
                        self._increment(self.zone_cache, znum, seconds)

            cmf = CounterMetricFamily(
                'watering_duration_sec_total', 'number of seconds of watering',
                labels=['deviceId', 'nameLabel', 'zone', 'zonename'],
                created=self.clientstarttime
            )
            for z in allzones:
                if z.get('enabled', False):
                    znum = z['zoneNumber']
                    zname = z.get('name', '')
                    labelvalues = [devid, devname, str(znum), zname]
                    with self.cache_cv:
                        # must hold the lock as another thread may have
                        # created a new zonename-to-zonenumber entry
                        cmf.add_metric(labelvalues, self.zone_cache.get(znum, 0))

        return [v for v in metric_to_gauge.values()] + [cmf]


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
