# rinnai.py
#
# the Rinnai module for porter, the Prometheus exporter
#
# see https://github.com/explosivo22/rinnaicontrolr

import logging, prometheus_client, requests, time, threading
import rinnaicontrolr

from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('rinnai_processing_seconds',
                                         'time of rinnai requests')

LOGGER = logging.getLogger('porter.rinnai')

class RinnaiError(Exception):
    pass
  
class RinnaiClient:
    def __init__(self, config):
        self.config = config
        self.cv = threading.Condition()
        self.emailtoclient = {}
        self.emailtocache = {}

        myconfig = config.get('rinnai')
        if not myconfig:
            raise Exception('no rinnai configuration')
        if not myconfig.get('credentials'):
            raise Exception('no rinnai credentials')
        if not myconfig.get('timeout'):
            myconfig['timeout'] = 10
        if not myconfig.get('cachetime'):
            myconfig['cachetime'] = 0

    def get_devices(self, target):
        """Returns a list of dictionaries, one dictionary per device."""
        with self.cv:
            c = self.emailtoclient.get(target)
            if not c:
                pwd = self.config['rinnai']['credentials'].get(target)
                if not pwd:
                    raise RinnaiError(f'no credentials for target {target}')
                c = rinnaicontrolr.RinnaiWaterHeater(target, pwd)
                self.emailtoclient[target] = c
            now = time.time()
            (t, cache) = self.emailtocache.get(target, (0, []))
            if (now - t) > self.config['rinnai']['cachetime']:
                self.emailtocache[target] = (now, c.get_devices())
            return self.emailtocache[target][1]

    async def run(self, target, selector, command, *args):
        """For the first matching target, run command(args) on the device
        that matches selector."""
        def is_selected(deviceid, name):
            return str(selector) == str(deviceid) or str(name).startswith(str(selector))
        for device in self.get_devices(target):
            devid = device['id']
            devname = device.get('device_name', '')
            with self.cv:
                if is_selected(devid, devname):
                    c = self.emailtoclient.get(target)
                    assert c, target # self.get_devices() above ensures this
                    if command == 'start_recirculation':
                        LOGGER.info(f'start_recirculation {device["device_name"]} {args}')
                        c.start_recirculation(device, int(args[0]))
                    else:
                        LOGGER.warning(f'unknown command {command} for {target} {selector}')
                    return
        LOGGER.warning(f'run() on {target} selected empty set {selector}')

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

        for device in self.get_devices(target):
            devid = device['id']
            devname = device.get('device_name', '')
            labelvalues = [devid, devname]

            info = device.get('info', {})
            shadow = device.get('shadow', {})
            def maketemp(name, desc, val):
                g = makegauge(name, desc)
                g.add_metric(labelvalues, (float(info.get(val, '0')) - 32) * 5.0 / 9.0)

            g = makegauge('is_heating', '1 if device is currently heating, 0 otherwise')
            g.add_metric(labelvalues, info.get('domestic_combustion', 'false').lower() == 'true')
            g = makegauge('is_recirculating', '1 if device is currently recirculating, 0 otherwise')
            g.add_metric(labelvalues, 1 if shadow.get('recirculation_enabled', False) else 0)
            g = makegauge('operation_enabled', '1 if operation is enabled, 0 otherwise')
            g.add_metric(labelvalues, 1 if shadow.get('operation_enabled', False) else 0)
            g = makegauge('schedule_enabled', '1 if schedule is enabled, 0 otherwise')
            g.add_metric(labelvalues, 1 if shadow.get('schedule_enabled', False) else 0)
            g = makegauge('lock_enabled', '1 if lock is enabled, 0 otherwise')
            g.add_metric(labelvalues, 1 if shadow.get('lock_enabled', False) else 0)
            g = makegauge('priority_enabled', '1 if priority status enabled, 0 otherwise')
            g.add_metric(labelvalues, 1 if shadow.get('priority_status', False) else 0)
            g = makegauge('flow_control_state', '1 usually')
            g.add_metric(labelvalues, int(info.get('m07_water_flow_control_position')))
            g = makegauge('update_time', 'last updated')
            g.add_metric(labelvalues, 1000*float(info.get('unix_time')))
            maketemp('setpoint_c', 'outlet temperature setpoint, degrees Celsius', 'domestic_temperature')
            maketemp('inlet_c', 'inlet temperature, degrees Celsius', 'm08_inlet_temperature')
            maketemp('heat_exchanger_outlet_c', 'heat exchanger outlet temperature, degrees Celsius', 'm11_heat_exchanger_outlet_temperature')
            maketemp('outlet_c', 'outlet temperature, degrees Celsius', 'm02_outlet_temperature')

        return [v for v in metric_to_gauge.values()]


if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = RinnaiClient(config)
    target = sys.argv[2]

    i = client.get_devices(target)
    print(json.dumps(i, indent=2))
