# flo.py
#
# the Flo by Moen module for porter, the Prometheus exporter
#
# see https://github.com/rsnodgrass/pyflowater


import logging, requests, prometheus_client, pyflowater, threading, time
from datetime import datetime, timedelta, timezone
from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

LOGGER = logging.getLogger('porter.flo')

REQUEST_TIME = prometheus_client.Summary('flo_processing_seconds',
                                         'time of flo requests')

class FloException(Exception):
    pass

# Flo can be queried for consumption within a time window but cannot
# be queried for a counter directly (total gallons consumed). We
# maintain a counter metric for each device, with its value starting
# at 0 at the time we are created. For each hour that elapses after we
# are created, we request the hourly consumption and add it to our
# counter at that timestamp.

class Consumption:
    def __init__(self, deviceid, macaddress, location, devicenickname, client_start_timestamp):
        self.deviceid = deviceid
        self.labelvalues = [macaddress, location, devicenickname or '']
        self.end_timestamp = client_start_timestamp
        self.value = 0
        self.cv = threading.Condition()

    def add_metric(self, cmf):
        with self.cv:
            cmf.add_metric(self.labelvalues, self.value)

    def get_end_timestamp(self):
        with self.cv:
            return self.end_timestamp

    def append(self, timestamp, gallons):
        assert timestamp >= self.end_timestamp, (timestamp, self.end_timestamp)
        if gallons:
            self.end_timestamp = timestamp
            self.value += gallons
        LOGGER.debug(f'added {gallons}, total {self.value} gal ending {datetime.fromtimestamp(timestamp)}')

    def fetch_and_append(self, target_pyflo, lasttime):
        with self.cv:
            return self._fetch_and_append_locked(target_pyflo, lasttime)

    def _fetch_and_append_locked(self, target_pyflo, lasttime):
        start = datetime.fromtimestamp(self.get_end_timestamp(), tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        end = lasttime.replace(minute=0, second=0, microsecond=0).astimezone(tz=timezone.utc)
        if end - start < timedelta(hours=1):
            return # nothing to do yet

        LOGGER.debug(f'consumption check from {start.isoformat()} to {end.isoformat()}')
        c = target_pyflo.consumption(self.deviceid, startDate=start, endDate=end)

        last_block_ended = 0
        for block in c['items']:
            block_start = block.get('time')
            block_gal = block.get('gallonsConsumed')

            if block_start and block_gal is not None:
                block_ended = (isoparse(block_start) + timedelta(hours=1)).timestamp()
                if block_gal:
                    self.append(block_ended, block_gal)
                last_block_ended = max(last_block_ended, block_ended)
        if last_block_ended and last_block_ended - self.end_timestamp > 3600*8:
            LOGGER.debug(f'at {datetime.now().isoformat()} moving end_timestamp from {datetime.fromtimestamp(self.end_timestamp).isoformat()} to {datetime.fromtimestamp(last_block_ended - 3600*8).isoformat()}')
            self.end_timestamp = last_block_ended - 3600*8


class FloClient:
    def __init__(self, config):
        self.config = config
        floconfig = config.get('flo')
        if not floconfig:
            raise FloException('no flo configuration')
        creds = floconfig.get('credentials')
        if not creds:
            raise FloException('no credentials in config file')
        self.pyflomap = { user: pyflowater.PyFlo(user, password) for (user, password) in creds.items() }
        for (user, flo) in self.pyflomap.items():
            flo.save_password(creds[user])
        self.refresh_time = 0 # get locations immediately
        self.clientstarttime = time.time() - 3600
        self.deviceid_to_consumption = {}
        self.locations_cv = threading.Condition()
        self.consumption_cv = threading.Condition()

    def _get_consumption_object(self, deviceid, macaddress, location, devicenickname):
        with self.consumption_cv:
            cons = self.deviceid_to_consumption.get(deviceid)
            if not cons:
                cons = Consumption(deviceid, macaddress, location, devicenickname, self.clientstarttime)
                self.deviceid_to_consumption[deviceid] = cons
            return cons

    @REQUEST_TIME.time()
    def collect(self, target):
        """get the status of each device at each location"""

        with self.locations_cv:
            target_pyflo = self.pyflomap.get(target)
            if target_pyflo is None:
                raise FloException(f'no config credentials for target {target}')
            if time.time() > self.refresh_time:
                self.locationsmap = { targ: pyf.locations(use_cached=False) for (targ, pyf) in self.pyflomap.items() }
                self.refresh_time = time.time() + 86400
            locations = [loc.copy() for loc in self.locationsmap[target]]

        cmf = CounterMetricFamily('water_used_gal',
                                  'water consumption through this valve (gal)',
                                  labels=['macAddress', 'location', 'nameLabel'],
                                  created=self.clientstarttime)
        metric_to_gauge = {}

        for loc in locations:
            locname = loc.get('nickname', '')
            locmode = loc.get('systemMode')
            notif = loc.get('notifications', {})
            warningcount = int(notif.get('pending', {}).get('warningCount', 0))
            criticalcount = int(notif.get('pending', {}).get('criticalCount', 0))
            for device in loc['devices']:
                deviceid = device['id']
                d = target_pyflo.device(deviceid)
                dprops = d.get('fwProperties', {})
                dname = dprops.get('nickname')
                def makegauge(metric, desc, labels=[]):
                    already = metric_to_gauge.get(metric)
                    if already:
                        return already
                    dlab = ['nameLabel'] if dname else []
                    lab = ['macAddress', 'location'] + dlab + labels
                    gmf = GaugeMetricFamily(metric, desc, labels=lab)
                    metric_to_gauge[metric] = gmf
                    return gmf
                labelvalues = [d['macAddress'], locname] + ([dname] if dname else [])
                g = makegauge('up', '1 if device connected, 0 otherwise')
                g.add_metric(labelvalues, 1 if d.get('isConnected') else 0)
                modetarget = d.get('systemMode', {}).get('target', '')
                mode = d.get('systemMode', {}).get('lastKnown', '')
                g = makegauge('mode_occupied', '1 if home, 0 if away', labels=['target'])
                g.add_metric(labelvalues + [modetarget], 1 if mode == 'home' else 0)
                valvetarget = d.get('valve', {}).get('target', '')
                valve = d.get('valve', {}).get('lastKnown', '')
                g = makegauge('valve_open', '1 if valve is open, 0 if closed', labels=['target'])
                g.add_metric(labelvalues + [valvetarget], 1 if valve == 'open' else 0 if valve == 'closed' else -1)
                g = makegauge('sysUpTime', 'device-reported uptime')
                g.add_metric(labelvalues, 1000 * dprops['device_uptime_sec'])
                g = makegauge('wifi_disconnections', 'device-reported count')
                g.add_metric(labelvalues, dprops['wifi_disconnections'])
                g = makegauge('num_reboots', 'number of reboots')
                g.add_metric(labelvalues, dprops['reboot_count'])
                g = makegauge('valve_actuations', 'count of valve actuations')
                g.add_metric(labelvalues, dprops['valve_actuation_count'])

                telemetry = d.get('telemetry', {}).get('current', {})
                if telemetry:
                    g = makegauge('water_flow_gpm', 'flow rate (gal/min)')
                    g.add_metric(labelvalues, telemetry['gpm'])
                    g = makegauge('water_pressure_psi', 'mains pressure (psi)')
                    g.add_metric(labelvalues, telemetry['psi'])
                    tempc = round((float(telemetry['tempF'])-32)*5/9, 1)
                    g = makegauge('temp_c', 'ambient air temperature (degrees Celsius)')
                    g.add_metric(labelvalues, tempc)

                c = self._get_consumption_object(deviceid, d['macAddress'], locname, dname)
                c.fetch_and_append(target_pyflo, isoparse(d['lastHeardFromTime']))
                c.add_metric(cmf)
                
        return [v for v in metric_to_gauge.values()] + [cmf]


def print_consumption(myflo, id, dev):
    lasttime = isoparse(dev['lastHeardFromTime'])
    sd = lasttime.replace(minute=0, second=0, microsecond=0)
    ed = sd + timedelta(minutes=5)
    print(sd.isoformat(), ed.isoformat())
    print(json.dumps(myflo.consumption(id, startDate=sd, endDate=ed, interval=pyflowater.INTERVAL_HOURLY), indent=2))
    print(json.dumps(myflo.consumption(id, endDate=sd), indent=2))


def print_callbacks(myflo, device, dev):
    id = device['id']
    def callback(dict):
        print(time.ctime(), dict['telemetry']['current'])
    listener = myflo.get_real_time_listener(device['macAddress'], callback)
    listener._heartbeat_func = lambda: None
    #listener = myflo.get_real_time_listener(device['macAddress'], callback, heartbeat=False)
    listener.start()


if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = FloClient(config)
    myflo = next(iter(client.pyflomap.values())) # always do the first one
    loc = myflo.locations()
    print(json.dumps(loc, indent=2))
    for location in loc:
        for device in location['devices']:
            id = device['id']
            dev = myflo.device(id)
            print(json.dumps(dev, indent=2))
            ## print_consumption(myflo, id, dev)
            print_callbacks(myflo, device, dev)
