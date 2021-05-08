# flo.py
#
# the Flo by Moen module for porter, the Prometheus exporter
#
# see https://github.com/rsnodgrass/pyflowater


import requests, prometheus_client, pyflowater, time

from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('flo_processing_seconds',
                                         'time of flo requests')

class FloClient:
    def __init__(self, config):
        self.config = config
        floconfig = config.get('flo')
        if not floconfig:
            raise Exception('no flo configuration')
        if not floconfig.get('user'):
            raise Exception('no flo user')
        if not floconfig.get('password'):
            raise Exception('no flo password')
        if not floconfig.get('timeout'):
            floconfig['timeout'] = 10
        self.pyflo = pyflowater.PyFlo(floconfig.get('user'), floconfig.get('password'))
        self.refresh_time = 0 # get locations immediately

    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

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
                g = makegauge('water_temp_c', 'mains temperature (degrees Celsius)')
                g.add_metric(labelvalues, tempc)
                g = makegauge('valve_actuations', 'count of valve actuations')
                g.add_metric(labelvalues, dprops['valve_actuation_count'])
                
        return metric_to_gauge.values()

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
            print(json.dumps(client.pyflo.device(id), indent=2))
            print(json.dumps(client.pyflo.consumption(id), indent=2))
