# lutron.py
#
# the Lutron module for porter, the Prometheus exporter.
#    supports Radio Ra2 Select, Homeworks QS, and probably others
#
# see https://github.com/upsert/liplib
# and https://www.lutron.com/TechnicalDocumentLibrary/040249.pdf

import asyncio, json, liplib, prometheus_client, time, threading

from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('lutron_processing_seconds',
                                         'time of Lutron requests')

# for each target we have one "press_actions" counter with labels
# "area", "deviceid", "name", "button", and "scene_number", and one
# "output_level_pct" gauge with labels "area", "deviceid", "name".
#
# when the Lutron device tells us an output level has changed, we update the gauge.
# when the Lutron device tells us a press action has occurred, we increment the counter.
#
# a press action on deviceid 1 means that a scene was triggered, so we apply the scene_number
# label (area and button are empty). for other deviceids, scene_number is empty and button
# is the component that was pressed.


# Radio Ra2 Select supports the following parts of the Lutron Integration Protocol:
#   observe press and release of Pico buttons:
#      ~DEVICE,28,2,3
#      ~DEVICE,28,2,4
#         [various ~OUTPUT changes from the button action]
#   observe activation of scenes:
#      ~DEVICE,1,1,3
#         [various ~OUTPUT changes from the scene]
#      ~DEVICE,1,1,4
#   observe changes to output levels, e.g. from the app:
#      ~OUTPUT,23,1,0.00
#   query the output level of a dimmer:
#      ?output,26,1
#      returns ~OUTPUT,26,1,100.0
#   press and release a Pico button causing it to activate its associated outputs:
#      #device,28,2,3
#      #device,28,2,4
#   returns ~OUTPUT,26,1,100.00
#           ~OUTPUT,27,1,100.00
#           ~OUTPUT,23,1,100.00
#   run a scene
#      #device,1,2,3
#      #device,1,2,4

# LipServer supports all of that. We support all the monitoring above and also
# Homeworks QS monitoring of ~OUTPUT and ~SHADEGRP.

class ConfigParams:
    def __init__(self, toplutronconfig, target):
        self.lutronconfig = toplutronconfig.get(target) or toplutronconfig

        # we default to the username/password for Radio Ra2 Select
        if not self.lutronconfig.get('user'):
            self.lutronconfig['user'] = 'lutron'
        if not self.lutronconfig.get('password'):
            self.lutronconfig['password'] = 'integration'

        integration_json_string = self.lutronconfig.get('integration')
        if integration_json_string:
            self.process_integration_report(json.loads(integration_json_string))
        else:
            self._process_integration_yaml()

    def get(self, toplevel, default=None):
        return self.lutronconfig.get(toplevel, default)

    def process_integration_report(self, js):
        """Reads an integration report that was generated by the Lutron app for
        Radio Ra 2 Select. This overwrites any existing integration information from
        the config file or previous calls to this method.
        """
        lipidlist = js.get('LIPIdList')
        assert lipidlist, js

        self.sceneid_to_name = {} # all scenes are components of deviceid 1
        self.deviceid_to_sensortuple = {} # sensors (Pico or Occupancy Sensor)
        self.deviceid_to_dimmertuple = {}
        self.areaname_to_devices = {}
        def add_device(areaname, t):
            lis = self.areaname_to_devices.get(areaname)
            if not lis:
                lis = []
                self.areaname_to_devices[areaname] = lis
            lis.append(t)

        for device in lipidlist.get('Devices', []):
            if device['ID'] == 1: # then device is the Smart Bridge
                for button in device.get('Buttons', []):
                    name = button['Name']
                    if not name.startswith('Button '):
                        # then this "button" is a scene
                        self.sceneid_to_name[button['Number']] = name
            else: # then device is a sensor (Pico or occupancy sensor)
                areaname = device.get('Area', {}).get('Name', '')
                buttons = [b['Number'] for b in device.get('Buttons', [])]
                self.deviceid_to_sensortuple[device['ID']] = (device['Name'], areaname, buttons)
                add_device(areaname, (device['ID'], device['Name'], buttons))

        for device in lipidlist.get('Zones', []):
            areaname = device.get('Area', {}).get('Name', '')
            self.deviceid_to_dimmertuple[device['ID']] = (device['Name'], areaname)
            add_device(areaname, (device['ID'], device['Name']))

    def _process_integration_yaml(self):
        """This processes the scenes and areas maps from the config to create the
        deviceid maps. Never needs to be called by the client.
        """
        self.sceneid_to_name = self.lutronconfig.get('scenes', {})
        self.areaname_to_devices = self.lutronconfig.get('areas', {})
        self.deviceid_to_dimmertuple = {}
        self.deviceid_to_sensortuple = {}
        for (areaname, devicelist) in self.areaname_to_devices.items():
            for (deviceid, devicename, *buttons) in devicelist:
                if not buttons:
                    self.deviceid_to_dimmertuple[deviceid] = (devicename, areaname)
                else:
                    assert len(buttons) == 1, (deviceid, devicename, buttons)
                    self.deviceid_to_sensortuple[deviceid] = (devicename, areaname, buttons[0])

    def dump_integration_yaml_string(self):
        """Returns a YAML format string that reflects the integration. It's more compact
        and easier to read than the integration JSON.
        """
        out = ['lutron:']
        out.append('  scenes:')
        for (sceneid, name) in sorted(self.sceneid_to_name.items()):
            out.append("    %d: '%s'" % (sceneid, name))
        out.append('  areas:')
        for (areaname, t) in sorted(self.areaname_to_devices.items()):
            lis = ['[%s]' % ', '.join([str(devid), "'%s'" % dname] + [str(b) for b in buttons]) for (devid, dname, *buttons) in t]
            out.append(("    '%s': [" % areaname) + ', '.join(lis) + ']')
        return '\n'.join(out)

class Lipservice:
    def __init__(self, host, port, cfparams):
        self.lipserver = liplib.LipServer()
        self.host, self.port = host, port
        self.cfparams = cfparams
        self.last_ping = time.time()

        prompt = self.cfparams.get('prompt')
        if prompt:
            self.lipserver.prompt = prompt.encode() + b'> '
        
        self.counts_by_scene_number = {}
        self.counts_by_deviceid_component = {}
        self.outputlevels = {}
        for deviceid in self.cfparams.deviceid_to_dimmertuple.keys():
            self.outputlevels[deviceid] = None
        # Hold the lock if you are going to add a key to the three dictionaries
        # above, or if you are going to iterate over those dictionaries outside
        # of the async task thread.
        self.cv = threading.Condition()

        print('new Lutron connection to %s:%d' % (host, port))

    async def open(self):
        await self.lipserver.open(self.host, self.port,
                                  username=self.cfparams.get('user').encode(),
                                  password=self.cfparams.get('password').encode())
        self.last_ping = time.time()

    async def query_levels(self):
        await self.open()
        # no need to hold the lock because we are in the async task thread
        for deviceid in self.outputlevels:
            await self.lipserver.query('OUTPUT', deviceid, 1)

    async def ping(self):
        ping_timeout = 10*60
        while self.last_ping + ping_timeout > time.time():
            await asyncio.sleep(self.last_ping + ping_timeout - time.time())
        self.last_ping = time.time()
        await self.lipserver.ping()
        return self.ping()

    def _increment_counter(self, map_of_counters, key, increment=1):
        new_value = map_of_counters.get(key, 0) + increment
        with self.cv:
            map_of_counters[key] = new_value
        return new_value

    async def poll(self):
        (a, b, c, d) = await self.lipserver.read()
        if a is None:
            await self.open()
        elif a == 'DEVICE':
            deviceid, component, action = int(b), int(c), int(d)
            self.last_ping = time.time()
            if action == liplib.LipServer.Button.PRESS:
                if deviceid == 1: # then a scene was triggered
                    count = self._increment_counter(self.counts_by_scene_number, component)
                else: # a standalone device
                    count = self._increment_counter(self.counts_by_deviceid_component, (deviceid, component))
            # We ignore button releases even though that is where the action is taken.
            # Thus we also ignore double-press and long-press that can be reported
            # by Homeworks QS.
        elif a == 'OUTPUT' or a == 'SHADEGRP':
            # SHADEGRP is reported by Homeworks QS
            deviceid, action, level = b, c, d
            self.last_ping = time.time()
            if action == liplib.LipServer.Action.SET:
                self.outputlevels[deviceid] = level
            elif action == liplib.LipServer.Action.RAISING:
                # valid response, but outputlevel will be reported later so we ignore this
                pass
            elif action == liplib.LipServer.Action.LOWERING:
                # valid response, but outputlevel will be reported later so we ignore this
                pass
            elif action == liplib.LipServer.Action.STOP:
                pass
            elif action == 29 or action == 30:
                # These are reported by Homeworks QS and possibly others (though NOT
                # Radio Ra2 Select) and are not documented in the Homeworks Integration
                # Guide. I have no idea about action 30. For action 29:
                #    if value is 6, the previous change was caused by an integration command
                #       (i.e. something we might write)
                #    if value is 8, the change was caused by a keypad buttonpress
                #    if value is 10, it was caused by a motion sensor for "occupancy"
                #    if value is 11, it was caused by a motion sensor for "vacancy"
                pass
            else:
                print('unknown ~OUTPUT action %d for deviceid %d level %d' % (action, deviceid, level))
        elif a == 'GROUP':
            # emitted by Homeworks QS to show occupancy status of an occupancy sensor group.
            # param of 3 means occupied, 4 means unoccupied, 255 is unknown
            deviceid, action, param = b, c, d
            if action == 3:
                val = 100 if param == 3 else 0 if param == 4 else -1
                if val == -1:
                    print('unknown GROUP param %d for deviceid %d' % (param, deviceid))
                self.outputlevels[deviceid] = val
            else:
                print('unknown GROUP action %d %d for deviceid %d' % (action, param, deviceid))
        elif a == 'ERROR':
            print('~ERROR while polling: %s %s %s' % (b, c, d))
        else:
            print('unknown response while polling: %s %s %s %s' % (a, b, c, d))
        return self.poll() # return ourselves as coroutine so we are restarted

class LipserviceManager:
    def __init__(self):
        self.cv = threading.Condition()
        self.target_to_cfparams = {}
        self.hostport_to_lipservice = {}
        self.tasks_pending = set()

    def register_target(self, lutronconfig, target):
        with self.cv:
            cfparams = self.target_to_cfparams.get(target)
            if not cfparams:
                cfparams = ConfigParams(lutronconfig, target)
                self.target_to_cfparams[target] = cfparams
            return cfparams

    def get_lipservice_for_target(self, target):
        (host, colon, portstr) = target.partition(':')
        port = int(portstr or 23)
        return self.hostport_to_lipservice.get((host, port))

    async def poll(self, timeout=1):
        with self.cv:
            targets = [t for t in self.target_to_cfparams.items()]
        for (target, cfparam) in targets:
            (host, colon, portstr) = target.partition(':')
            port = int(portstr or 23)
            lips = self.hostport_to_lipservice.get((host, port))
            if not lips:
                lips = Lipservice(host, port, cfparam)
                self.hostport_to_lipservice[(host, port)] = lips
                asyncio.create_task(lips.query_levels()) # only do this once
                self.tasks_pending.add(asyncio.create_task(lips.poll()))
                self.tasks_pending.add(asyncio.create_task(lips.ping()))
        if self.tasks_pending:
            (done, self.tasks_pending) = await asyncio.wait(self.tasks_pending, timeout=timeout,
                                                            return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # as each poll() or ping() completes, schedule it to run again
                self.tasks_pending.add(asyncio.create_task(task.result()))
        else:
            await asyncio.sleep(timeout)


class LutronClient:
    def __init__(self, config):
        self.config = config
        self.manager = LipserviceManager()
        lutronconfig = config.get('lutron')
        if not lutronconfig:
            raise Exception('no lutron configuration')
        self.clientstarttime = time.time()

    async def poll(self):
        await self.manager.poll()
        return self.poll()

    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

        cfparams = self.manager.register_target(self.config['lutron'], target)
        lips = self.manager.get_lipservice_for_target(target)
        if not lips:
            return []
        gmf = GaugeMetricFamily('output_level_pct', 'current output level (% of full output)',
                                labels=['deviceId', 'name', 'area'])
        cmf = CounterMetricFamily(
            'press_actions', 'count of button presses and scene activations',
            labels=['deviceid', 'name', 'button', 'area', 'scene_number'],
            created=self.clientstarttime
        )
        with lips.cv:
            for deviceid in lips.outputlevels.keys():
                level = lips.outputlevels[deviceid]
                if level is not None:
                    (name, area) = cfparams.deviceid_to_dimmertuple.get(deviceid, ('', ''))
                    gmf.add_metric([str(deviceid), name, area], level)

            for (sceneid, count) in lips.counts_by_scene_number.items():
                name = lips.cfparams.sceneid_to_name.get(sceneid, '')
                cmf.add_metric(['', name, '', '', str(sceneid)], count, timestamp=time.time())
            for (tup, count) in lips.counts_by_deviceid_component.items():
                (deviceid, component) = tup
                (name, area, buttons) = lips.cfparams.deviceid_to_sensortuple.get(deviceid, (None, None, None))
                if name is None:
                    (name, area) = lips.cfparams.deviceid_to_dimmertuple.get(deviceid, (None, None))
                cmf.add_metric([str(deviceid), name, str(component), area],
                               count, timestamp=time.time())

        return [gmf, cmf]

if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = LutronClient(config)
    cfparams = ConfigParams(config.get('lutron'), 'ignored')

    js = json.load(open(sys.argv[2], 'rt')) # the integration report from the bridge
    cfparams.process_integration_report(js)
    s = cfparams.dump_integration_yaml_string()
    print(s)

    #devices = liplib.load_integration_report(js)
    #print(json.dumps(devices, indent=2))
