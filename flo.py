# flo.py
#
# the Flo by Moen module for porter, the Prometheus exporter
#
# see https://github.com/rsnodgrass/pyflowater


import requests, prometheus_client, pyflowater, threading, time
from datetime import datetime, timedelta
from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('flo_processing_seconds',
                                         'time of flo requests')

# Flo can be queried for consumption within a time window but cannot be queried for a
# counter directly (total gallons consumed). We maintain a counter metric for each device,
# with its value starting at 0 at the time we are created. For each minute that elapses
# after we are created, we request the minutely consumption within that minute and
# add it to our counter at that timestamp.

class Consumption:
    def __init__(self, deviceid, macaddress, location, devicenickname, client_start_timestamp):
        self.deviceid = deviceid
        labels = ['macAddress', 'location']
        self.labelvalues = [macaddress, location]
        if devicenickname:
            labels.append('nameLabel')
            self.labelvalues.append(devicenickname)
        self.cmf = CounterMetricFamily('water_used_gal',
                                       'water consumption through this valve (gal)',
                                       labels=labels,
                                       created=client_start_timestamp)
        self.end_timestamp = client_start_timestamp
        self.value = 0

    def get_end_timestamp(self):
        return self.end_timestamp

    def append(self, timestamp, gallons):
        assert timestamp >= self.end_timestamp, (timestamp, self.end_timestamp)
        self.end_timestamp = timestamp
        self.value += gallons
        self.cmf.add_metric(self.labelvalues, self.value, timestamp=timestamp)

    def fetch_and_append(self, floclient, lasttime):
        #start = datetime.fromtimestamp(self.get_end_timestamp()).replace(second=0)
        #end = datetime.now().replace(second=0)
        #c = floclient.pyflo.consumption(self.deviceid, startDate=start,
        #                                endDate=end-timedelta(microseconds=1),
        #                                interval=pyflowater.INTERVAL_MINUTE)

        start = datetime.fromtimestamp(self.get_end_timestamp()).replace(minute=0, second=0, microsecond=0)
        end = datetime.fromtimestamp(lasttime.replace(minute=0, second=0, microsecond=0).timestamp())
        if end - timedelta(hours=1) < start:
            return # nothing to do yet

        c = floclient.pyflo.consumption(self.deviceid, startDate=start, endDate=end)

        last_block_ended = None
        for block in c['items']:
            block_start = block.get('time')
            block_gal = block.get('gallonsConsumed')

            if block_start and block_gal is not None:
                last_block_ended = (isoparse(block_start) + timedelta(hours=1)).timestamp()
                self.append(last_block_ended, block_gal)
        if last_block_ended:
            self.end_timestamp = last_block_ended


class FloClient:
    def __init__(self, config):
        self.config = config
        floconfig = config.get('flo')
        if not floconfig:
            raise Exception('no flo configuration')
        if not floconfig.get('user'):
            raise Exception('no flo user')
        password = floconfig.get('password')
        if not password:
            raise Exception('no flo password')
        self.pyflo = pyflowater.PyFlo(floconfig.get('user'), password)
        self.pyflo.save_password(password)
        self.refresh_time = 0 # get locations immediately
        self.clientstarttime = time.time() - 3600
        self.deviceid_to_consumption = {}
        self.consumption_cv = threading.Condition()

    def _get_consumption_object(self, deviceid, macaddress, location, devicenickname):
        with self.consumption_cv:
            cmf = self.deviceid_to_consumption.get(deviceid)
            if not cmf:
                cmf = Consumption(deviceid, macaddress, location, devicenickname,
                                  self.clientstarttime)
                self.deviceid_to_consumption[deviceid] = cmf
            return cmf

    @REQUEST_TIME.time()
    def collect(self, target):
        """get the status of each device at each location"""

        floconfig = self.config['flo']
        if time.time() > self.refresh_time:
            self.locations = self.pyflo.locations()
            self.refresh_time = time.time() + 86400

        metric_to_gauge = {}

        for loc in self.locations:
            locname = loc.get('nickname', '')
            locmode = loc.get('systemMode')
            notif = loc.get('notifications', {})
            warningcount = int(notif.get('pending', {}).get('warningCount', 0))
            criticalcount = int(notif.get('pending', {}).get('criticalCount', 0))
            for device in loc['devices']:
                deviceid = device['id']
                d = self.pyflo.device(deviceid)
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
                g = makegauge('uptime_sec', 'device-reported uptime')
                g.add_metric(labelvalues, dprops['device_uptime_sec'])
                g = makegauge('wifi_disconnections', 'device-reported count')
                g.add_metric(labelvalues, dprops['wifi_disconnections'])
                g = makegauge('water_flow_gpm', 'flow rate (gal/min)')
                g.add_metric(labelvalues, dprops['telemetry_flow_rate'])
                g = makegauge('water_pressure_psi', 'mains pressure (psi)')
                g.add_metric(labelvalues, dprops['telemetry_pressure'])
                tempc = round((float(dprops['telemetry_temperature'])-32)*5/9, 1)
                g = makegauge('temp_c', 'ambient air temperature (degrees Celsius)')
                g.add_metric(labelvalues, tempc)
                g = makegauge('valve_actuations', 'count of valve actuations')
                g.add_metric(labelvalues, dprops['valve_actuation_count'])

                c = self._get_consumption_object(deviceid, d['macAddress'], locname, dname)
                c.fetch_and_append(self, isoparse(d['lastHeardFromTime']))
                
        return [v for v in metric_to_gauge.values()] + [c.cmf for c in self.deviceid_to_consumption.values()]

if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = FloClient(config)
    loc = client.pyflo.locations()
    print(json.dumps(loc, indent=2))
    for location in loc:
        for device in location['devices']:
            id = device['id']
            dev = client.pyflo.device(id)
            print(json.dumps(dev, indent=2))
            lasttime = isoparse(dev['lastHeardFromTime'])
            sd = lasttime.replace(minute=0, second=0, microsecond=0)
            ed = sd + timedelta(minutes=5)
            print(sd.isoformat(), ed.isoformat())
            print(json.dumps(client.pyflo.consumption(id, startDate=sd, endDate=ed, interval=pyflowater.INTERVAL_MINUTE), indent=2))
            print(json.dumps(client.pyflo.consumption(id, endDate=sd), indent=2))
