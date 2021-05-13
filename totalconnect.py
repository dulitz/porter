# totalconnect.py
#
# the TotalConnect module for porter, the Prometheus exporter
#
# see https://github.com/craigjmidwinter/total-connect-client


import requests, prometheus_client, threading, time
from datetime import datetime, timedelta
from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily
from total_connect_client import TotalConnectClient as TCC
#import totalconnectupstream as TCC

REQUEST_TIME = prometheus_client.Summary('totalconnect_processing_seconds',
                                         'time of totalconnect requests')

class TotalConnectClient:
    def __init__(self, config):
        self.config = config
        tcconfig = config.get('totalconnect')
        if not tcconfig:
            raise Exception('no totalconnect configuration')
        user = tcconfig.get('user')
        if not user:
            raise Exception('no totalconnect user')
        password = tcconfig.get('password')
        if not password:
            raise Exception('no totalconnect password')
        self.client = None # creating this takes time so wait until the first request
        self.cv = threading.Condition()

    def statename_for_arming_status(self, status):
        if status == 10200:
            return 'disarmed'
        elif status == 10211:
            return 'disarmed-bypass'
        elif status == 10201:
            return 'armed away'
        elif status == 10202:
            return 'armed away bypass'
        elif status == 10205:
            return 'armed away instant'
        elif status == 10206:
            return 'armed away instant bypass'
        elif status == 10223:
            return 'armed custom bypass'
        elif status == 10203:
            return 'armed stay'
        elif status == 10204:
            return 'armed stay bypass'
        elif status == 10209:
            return 'armed stay instant'
        elif status == 10210:
            return 'armed stay instant bypass'
        elif status == 10218:
            return 'armed stay night'
        elif status == 10307:
            return 'arming'
        elif status == 10308:
            return 'disarming'
        elif status == 10207:
            return 'alarming'
        elif status == 10212:
            return 'alarming fire smoke'
        elif status == 10213:
            return 'alarming carbon monoxide'
        else:
            return 'unknown status %s' % status
        
    @REQUEST_TIME.time()
    def collect(self, target):
        """get the status of each device at each location"""

        tcconfig = self.config['totalconnect']
        fresh_data = False
        with self.cv:
            if not self.client:
                fresh_data = True
                print('connecting to TotalConnect...')
                self.client = TCC.TotalConnectClient(tcconfig['user'], tcconfig['password'])
                print('connected to TotalConnect')

        metric_to_gauge = {}
        for loc in self.client.locations.values():
            with self.cv:
                if not fresh_data:
                    self.client.get_panel_meta_data(loc.location_id)
                self._collect_from_location(loc, metric_to_gauge)
        return [v for v in metric_to_gauge.values()]

    def _collect_from_location(self, loc, metric_to_gauge):
        def makegauge(metric, desc, labels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            lab = ['locationId', 'location'] + labels
            gmf = GaugeMetricFamily(metric, desc, labels=lab)
            metric_to_gauge[metric] = gmf
            return gmf
        labelvalues = [str(loc.location_id), loc.location_name]
        g = makegauge('ac_power_lost', '1 if AC power lost, 0 if good')
        g.add_metric(labelvalues, 1 if loc.ac_loss else 0)
        g = makegauge('low_battery', '1 if battery low, 0 if good')
        g.add_metric(labelvalues, 1 if loc.low_battery else 0)
        g = makegauge('cover_tampered', '1 if cover tampered, 0 if good')
        g.add_metric(labelvalues, 1 if loc.cover_tampered else 0)
        g = makegauge('arming_state', 'arming state of this location', labels=['state'])
        g.add_metric(labelvalues + [self.statename_for_arming_status(loc.arming_state)],
                     loc.arming_state)
        g = makegauge('last_updated', 'timestamp of last update time')
        g.add_metric(labelvalues, loc.last_updated_timestamp_ticks / 1000000.0)

        for (i, zone) in sorted(loc.zones.items()):
            labels = ['zoneid', 'zonename', 'partition']
            labelvalues2 = labelvalues + [str(i), zone.description, str(zone.partition)]
            status = 'alarm' if zone.is_triggered() else 'bypass' if zone.is_bypassed() else 'fault' if zone.is_faulted() else 'tamper' if zone.is_tampered() else 'low battery' if zone.is_low_battery() else 'ok'
            g = makegauge('alarm_zone_status', 'status of alarm zone',
                          labels=(labels+['state']))
            g.add_metric(labelvalues2 + [status], zone.status)
            g = makegauge('alarm_zone_type', 'type of alarm zone', labels=(labels+['zonetype']))
            t = 'button' if zone.is_type_button() else 'security' if zone.is_type_security() else 'motion' if zone.is_type_motion() else 'fire' if zone.is_type_fire() else 'carbon monoxide' if zone.is_type_carbon_monoxide() else 'unknown'
            g.add_metric(labelvalues2 + [t], zone.zone_type_id)
            g = makegauge('alarm_zone_can_bypass', '1 if zone can be bypassed, 0 otherwise',
                          labels=labels)
            g.add_metric(labelvalues2, zone.can_be_bypassed)

if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = TotalConnectClient(config)
    print(str(client.client))